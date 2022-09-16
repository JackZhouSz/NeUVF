import os
import torch
import imageio
import numpy as np
import math
import torch.nn as nn
import time
from tensorboardX import SummaryWriter
from NeRF import *

from configs import config_parser
from dataloader import load_data, load_images, load_masks, load_position_maps, has_matted, load_matted
from utils import *
import shutil
from datetime import datetime
from metrics import compute_img_metric
import cv2
import mcubes
import trimesh
from PIL import Image

torch.set_default_tensor_type('torch.cuda.FloatTensor')
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


if __name__ == "__main__":
    parser = config_parser()
    parser.add_argument("--resolutionh", type=int, default=100,
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--resolutionw", type=int, default=100,
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--resolutiond", type=int, default=100,
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--t", type=int, default=0,
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--iso_value", type=float, default=5,
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--texture", type=str, default="assets/checker.png",
                        help='can be "rgb", <path_to_texture>')

    args = parser.parse_args()
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    imgpaths, poses, intrinsics, bds, render_poses, render_intrinsics = load_data(datadir=args.datadir,
                                                                                  factor=args.factor,
                                                                                  bd_factor=args.bd_factor,
                                                                                  frm_num=args.frm_num)
    T = len(imgpaths)
    V = len(imgpaths[0])
    H, W = imageio.imread(imgpaths[0][0]).shape[0:2]
    print('Loaded llff', T, V, H, W, poses.shape, intrinsics.shape, render_poses.shape, render_intrinsics.shape,
          bds.shape)
    args.time_len = T
    #######
    # load uv map
    uv_gts = None
    basenames = [os.path.basename(ps_[0]).split('.')[0] for ps_ in imgpaths]
    period = args.uv_map_gt_skip_num + 1
    basenames = basenames[::period]
    uv_gt_id2t = np.arange(0, T, period)
    assert (len(uv_gt_id2t) == len(basenames))
    t2uv_gt_id = np.repeat(np.arange(len(basenames)), period)[:T]
    if args.uv_loss_weight > 0:
        uv_gts = load_position_maps(args.datadir, args.factor, basenames)
        uv_gts = torch.tensor(uv_gts).cuda()
        # transform uv from (0, 1) to (- uv_map_face_roi,  uv_map_face_roi)
        uv_gts[..., 3:] = uv_gts[..., 3:] * (2 * args.uv_map_face_roi) - args.uv_map_face_roi

        args.uv_gts = uv_gts
        args.t2uv_gt_id = t2uv_gt_id
    nerf = NeUVFModulateT(args)
    ##########################
    # Load checkpoints
    ckpts = [os.path.join(args.expdir, args.expname, f)
             for f in sorted(os.listdir(os.path.join(args.expdir, args.expname))) if 'tar' in f]
    print('Found ckpts', ckpts)

    start = 0
    if len(ckpts) > 0 and not args.no_reload:
        ckpt_path = ckpts[-1]
        print('Reloading from', ckpt_path)
        ckpt = torch.load(ckpt_path)

        start = ckpt['global_step']
        smart_load_state_dict(nerf, ckpt)
        if 'rot_raw' in ckpt.keys():
            print("Loading poses and intrinsics from the ckpt")
            rot_raw = ckpt['rot_raw']
            tran_raw = ckpt['tran_raw']
            intrin_raw = ckpt['intrin_raw']
            poses, intrinsics = raw2poses(
                torch.cat([rot_raw0, rot_raw]),
                torch.cat([tran_raw0, tran_raw]),
                torch.cat([intrin_raw0, intrin_raw]))
            assert len(rot_raw) + 1 == V
    render_kwargs_train = {
        'N_samples': args.N_samples,
        'N_importance': args.N_importance,
        'use_viewdirs': args.use_viewdirs,
        'perturb': args.perturb,
        'raw_noise_std': args.raw_noise_std,
    }

    render_kwargs_test = {k: render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test['perturb'] = False
    render_kwargs_test['raw_noise_std'] = 0.

    bds_dict = {
        'box': bds
    }
    print(bds_dict)

    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)
    global_step = start

    # ##################################################################################################
    print("Scripting::Finish loading everything!!!")
    savedir = os.path.join(args.expdir, args.expname, f'mesh')
    os.makedirs(savedir, exist_ok=True)

    print("Scripting::saving objs")
    suffix = ""
    explicit_warp: WarpKptAdvanced = nerf.explicit_warp
    if args.render_canonical:
        print("Rendering canonical, setting explicit_warp to None")
        suffix += "_canonical"
        nerf.explicit_warp = None
        bds = np.array([[-0.65, -1, -0.8], [0.65, 0.7, 0.45]])

    batch_size = args.batch_size * 4

    def mcube2nerf(xyz):
        orishape = xyz.shape
        xyz = xyz.reshape(-1, 3)
        bds_t = torch.tensor(bds).type_as(xyz)
        scale = torch.tensor([args.resolutionw, args.resolutionh, args.resolutiond]).type_as(xyz)
        xyz = (xyz / scale[None, :]) * (bds_t[1:2] - bds_t[0:1]) + bds_t[0:1]
        return xyz.reshape(orishape)

    with torch.no_grad():
        xs = torch.arange(args.resolutionw)
        ys = torch.arange(args.resolutionh)
        zs = torch.arange(args.resolutiond)
        # xs = torch.linspace(0, 1, args.resolutionw) * (bds[1, 0] - bds[0, 0]) + bds[0, 0]
        # ys = torch.linspace(0, 1, args.resolutionh) * (bds[1, 1] - bds[0, 1]) + bds[0, 1]
        # zs = torch.linspace(0, 1, args.resolutiond) * (bds[1, 2] - bds[0, 2]) + bds[0, 2]
        grid = torch.meshgrid(xs, ys, zs)
        dist = zs[1] - zs[0]
        pts = torch.stack(grid, dim=-1).reshape(-1, 3).float()
        pts = mcube2nerf(pts)
        viewdir = torch.tensor([0, 0, 1.]).type_as(pts)
        densitys = []
        for batch_idx in range(0, len(pts), batch_size):
            print(f'\r {batch_idx / len(pts) * 100:.3f}%', end='')
            pts_batch = pts[batch_idx: batch_idx + batch_size]
            viewdirs = viewdir.reshape(-1, 3).expand_as(pts_batch)
            times = torch.ones_like(viewdirs[:, :1]) * args.t
            raw, uv, pts_warp, pts_out, alphas = nerf.mlpforward(pts_batch, viewdirs, times,
                                                                 nerf.mlp_fine, None)
            density = raw[..., -1]
            densitys.append(density)

        densitys = torch.cat(densitys).reshape(len(xs), len(ys), len(zs))
        densitys = densitys.cpu().numpy()

        print(f"runing marching cubes...")
        vertices, faces = mcubes.marching_cubes(densitys, args.iso_value)
        vertices = torch.tensor(vertices, dtype=torch.float32)
        vertices_world = mcube2nerf(vertices)

        vertices_world = torch.tensor(vertices_world)
        uvs = []
        for batch_idx in range(0, len(vertices_world), batch_size):
            print(f'\r {batch_idx / len(vertices_world) * 100:.3f}%', end='')
            vert_batch = vertices_world[batch_idx: batch_idx + batch_size]
            viewdirs = viewdir.reshape(-1, 3).expand_as(vert_batch)
            times = torch.ones_like(viewdirs[:, :1]) * args.t
            raw, uv, pts_warp, pts_out, alphas = nerf.mlpforward(vert_batch, viewdirs, times,
                                                                 nerf.mlp_fine, None)
            uvs.append(uv)
        uvs = torch.cat(uvs)
        uvs[:, 1] = -uvs[:, 1]
        uvs = (uvs + 1) / 2

        # used to perview the forward warping and backward warping difference
        # comment out this line to disable preview
        # vertices_world = explicit_warp.inverse_forward(vertices_world, args.t)

        # Save
        texture = Image.open(args.texture)
        texture = trimesh.visual.TextureVisuals(
            uv=uvs.cpu().numpy(),
            image=texture
        )
        mesh = trimesh.Trimesh(vertices=vertices_world.cpu().numpy(),
                               faces=faces,
                               visual=texture)
        meshes = mesh.split(only_watertight=False)
        num_vert = [len(m.vertices) for m in meshes]
        print(f"\n{len(meshes)} isolated mesh, vert nums = {num_vert}")
        select = np.argmax(num_vert)
        mesh = meshes[select]
        print("Saving")
        obj = trimesh.exchange.export.export_obj(mesh, include_texture=True)
        obj_path = os.path.join(savedir, f'mesh{suffix}.obj')
        with open(obj_path, 'w') as f:
            f.write(obj)
        print(f"Successfully saved to {savedir}")

        print("Scripting::saving cpts")
        canonical_pts = explicit_warp.kpt3d_canonical.cpu().numpy()
        control_pts = explicit_warp.kpt3d.cpu().numpy()
        radius = explicit_warp.kpt3d_bias_radius.cpu().numpy()
        transform = explicit_warp.transform.cpu().numpy()

        np.savez(os.path.join(savedir, "cpoint.npz"),
                 cpts=control_pts,
                 cano=canonical_pts,
                 radius=radius,
                 trans=transform,
                 frameidx=args.t)
        print(f"Successfully saved to {savedir}")
