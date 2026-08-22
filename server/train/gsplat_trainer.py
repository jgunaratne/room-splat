"""Real 3D Gaussian Splatting training on the GPU with gsplat (SPEC.md M2/M4/M6).

Loads a `.roomsplat` package through the shared parser, seeds Gaussians from
points3D.ply, and optimizes with gsplat's rasterizer + DefaultStrategy densification.
Isolated from `backends.py` so importing the backend interface never pulls in torch.

Coordinate handling is the whole ballgame (SPEC.md §5). transforms.json stores
camera-to-world in the Nerfstudio/OpenGL convention (camera looks -Z, +Y up). gsplat's
rasterizer wants world-to-camera in the OpenCV convention (camera looks +Z, +Y down),
so we flip the camera Y and Z axes and invert. Get this wrong and the room is mirrored
or inside-out — do not compensate with hyperparameters.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

from gsplat import rasterization
from gsplat.strategy import DefaultStrategy

from roomsplat.coords import opengl_c2w_to_opencv_w2c
from roomsplat.package import RoomSplatPackage
from .gaussian import GaussianCloud, rgb_to_sh_dc

log = logging.getLogger("roomsplat.train")


class GsplatTrainer:
    def __init__(self, package_root: Path, device: str = "cuda", sh_degree: int = 3,
                 max_image_long_edge: int = 1600):
        self.device = torch.device(device)
        self.sh_degree = sh_degree
        self.max_image_long_edge = max_image_long_edge
        self.package_root = Path(package_root)
        # Do not validate() here: during live streaming the package is mid-write (frames
        # still arriving). The offline driver validates before constructing the backend.
        self.pkg = RoomSplatPackage.open(package_root)
        self._iters = 0
        self._init_camera_intrinsics()
        self._init_gaussians()               # scene_scale from the seed cloud extent
        self._init_optimizers_and_strategy()
        # Views are loaded incrementally so live streaming can keep adding keyframes
        # (SPEC.md §4: never restart training on arrival). refresh() picks up any new
        # frames the disk mirror has appended to the package.
        self.viewmats = torch.empty(0, 4, 4, device=self.device)
        self.images = torch.empty(0, self.H, self.W, 3, device=self.device)
        self._n_loaded = 0
        self.refresh()

    # ---- data ---------------------------------------------------------------

    def _init_camera_intrinsics(self) -> None:
        cam = self.pkg.camera
        # Scale intrinsics if we downscale images to the long-edge cap.
        scale = min(1.0, self.max_image_long_edge / max(cam.w, cam.h))
        self.W = int(round(cam.w * scale))
        self.H = int(round(cam.h * scale))
        K = torch.tensor([[cam.fl_x, 0, cam.cx], [0, cam.fl_y, cam.cy], [0, 0, 1]], dtype=torch.float32)
        K[:2] *= scale
        self.K = K.to(self.device)

    def refresh(self) -> int:
        """Load any keyframes appended to the package since the last call.

        Re-opens the package (the disk mirror rewrites transforms.json atomically after
        every append, §6) and appends new views. Returns the number newly loaded.
        """
        pkg = RoomSplatPackage.open(self.package_root)
        frames = pkg.frames
        new = frames[self._n_loaded:]
        if not new:
            return 0
        viewmats, images = [], []
        for fr in new:
            w2c = opengl_c2w_to_opencv_w2c(np.asarray(fr.transform_matrix))
            viewmats.append(torch.from_numpy(w2c.astype(np.float32)))
            img = Image.open(self.package_root / fr.file_path).convert("RGB")
            if img.size != (self.W, self.H):
                img = img.resize((self.W, self.H), Image.BILINEAR)
            images.append(torch.from_numpy(np.asarray(img, dtype=np.float32) / 255.0))
        vm = torch.stack(viewmats).to(self.device)
        im = torch.stack(images).to(self.device)
        self.viewmats = torch.cat([self.viewmats, vm], dim=0)
        self.images = torch.cat([self.images, im], dim=0)
        self._n_loaded = len(frames)
        log.info("loaded %d new keyframes (%d total)", len(new), self._n_loaded)
        return len(new)

    def _init_gaussians(self) -> None:
        xyz, rgb = self.pkg.read_point_cloud()
        means = torch.from_numpy(xyz.astype(np.float32))
        rgb01 = torch.from_numpy(rgb.astype(np.float32) / 255.0)
        n = len(means)
        # scene_scale from the seed cloud extent: frame-independent, so learning rates
        # are stable whether frames are all present (offline) or trickling in (live).
        extent = (means.max(0).values - means.min(0).values).norm()
        self.scene_scale = float((0.5 * extent).clamp(min=1e-3))
        # Init scale from local spacing so densification starts from a sane size.
        dist = self._mean_neighbor_dist(means).clamp(min=1e-4)
        scales = torch.log(dist)[:, None].repeat(1, 3)
        quats = torch.zeros(n, 4); quats[:, 0] = 1.0
        opacities = torch.logit(torch.full((n,), 0.1))

        dim_sh = (self.sh_degree + 1) ** 2
        sh = torch.zeros(n, dim_sh, 3)
        sh[:, 0, :] = torch.from_numpy(rgb_to_sh_dc(rgb01.numpy()))

        dev = self.device
        self.params = torch.nn.ParameterDict({
            "means": torch.nn.Parameter(means.to(dev)),
            "scales": torch.nn.Parameter(scales.to(dev)),
            "quats": torch.nn.Parameter(quats.to(dev)),
            "opacities": torch.nn.Parameter(opacities.to(dev)),
            "sh0": torch.nn.Parameter(sh[:, :1, :].to(dev)),
            "shN": torch.nn.Parameter(sh[:, 1:, :].to(dev)),
        })

    def _mean_neighbor_dist(self, means: torch.Tensor, k: int = 4, cap: int = 50_000) -> torch.Tensor:
        # Estimate spacing from a random subset to keep it O(cap^2), broadcast back.
        n = len(means)
        idx = torch.randperm(n)[: min(n, cap)]
        sub = means[idx]
        d = torch.cdist(sub, sub)
        d.fill_diagonal_(float("inf"))
        knn = d.topk(min(k, len(sub) - 1), largest=False).values.mean(1)
        return torch.full((n,), float(knn.mean()))

    # ---- optim + strategy ---------------------------------------------------

    def _init_optimizers_and_strategy(self) -> None:
        s = self.scene_scale
        lrs = {
            "means": 1.6e-4 * s,
            "scales": 5e-3,
            "quats": 1e-3,
            "opacities": 5e-2,
            "sh0": 2.5e-3,
            "shN": 2.5e-3 / 20,
        }
        self.optimizers = {
            k: torch.optim.Adam([{"params": self.params[k], "lr": lr, "name": k}], eps=1e-15)
            for k, lr in lrs.items()
        }
        self.strategy = DefaultStrategy(verbose=False)
        self.strategy_state = self.strategy.initialize_state(scene_scale=s)
        self.strategy.check_sanity(self.params, self.optimizers)

    # ---- training -----------------------------------------------------------

    def _rasterize(self, cam_ids: torch.Tensor):
        sh = torch.cat([self.params["sh0"], self.params["shN"]], dim=1)
        renders, alphas, info = rasterization(
            means=self.params["means"],
            quats=self.params["quats"],
            scales=torch.exp(self.params["scales"]),
            opacities=torch.sigmoid(self.params["opacities"]),
            colors=sh,
            viewmats=self.viewmats[cam_ids],
            Ks=self.K[None].repeat(len(cam_ids), 1, 1),
            width=self.W,
            height=self.H,
            sh_degree=self.sh_degree,
            packed=False,
        )
        return renders, alphas, info

    def _sample_cam(self, rng: np.random.Generator, n: int) -> int:
        # 70% from the most recent 20 keyframes, 30% uniform (SPEC.md §4): puts gradient
        # where the new data is without letting earlier regions degrade.
        if n <= 20 or rng.random() < 0.3:
            return int(rng.integers(0, n))
        return int(rng.integers(n - 20, n))

    def step(self, n_iters: int) -> None:
        self.refresh()  # pick up keyframes streamed since the last tick
        n_cams = len(self.viewmats)
        if n_cams == 0:
            return  # nothing to train against yet (live: waiting on first keyframe)
        rng = np.random.default_rng()
        for _ in range(n_iters):
            step = self._iters
            cam_id = self._sample_cam(rng, n_cams)
            cam_ids = torch.tensor([cam_id], device=self.device)
            renders, _alphas, info = self._rasterize(cam_ids)
            self.strategy.step_pre_backward(self.params, self.optimizers, self.strategy_state, step, info)
            pred = renders[..., :3].clamp(0, 1)
            gt = self.images[cam_ids]
            loss = 0.8 * F.l1_loss(pred, gt) + 0.2 * (1 - _ssim(pred, gt))
            loss.backward()
            for opt in self.optimizers.values():
                opt.step()
                opt.zero_grad(set_to_none=True)
            self.strategy.step_post_backward(self.params, self.optimizers, self.strategy_state, step, info)
            self._iters += 1
            if step % 500 == 0:
                log.info("iter=%d loss=%.4f splats=%d", step, loss.item(), self.params["means"].shape[0])

    # ---- export -------------------------------------------------------------

    @property
    def total_iters(self) -> int:
        return self._iters

    def cloud(self) -> GaussianCloud:
        """DC-only view for the chunking/export pipeline (SPEC.md §4 cells)."""
        p = self.params
        return GaussianCloud(
            means=p["means"].detach().cpu().numpy(),
            colors_dc=p["sh0"].detach().cpu().numpy()[:, 0, :],
            opacity=p["opacities"].detach().cpu().numpy()[:, None] if p["opacities"].dim() == 1
            else p["opacities"].detach().cpu().numpy(),
            scales=p["scales"].detach().cpu().numpy(),
            quats=p["quats"].detach().cpu().numpy(),
        )

    def write_ply_full(self, path: Path) -> None:
        """Full-SH 3DGS PLY (view-dependent color) for the M2 deliverable."""
        p = self.params
        n = p["means"].shape[0]
        means = p["means"].detach().cpu().numpy()
        f_dc = p["sh0"].detach().cpu().numpy().reshape(n, -1)
        f_rest = p["shN"].detach().cpu().numpy().transpose(0, 2, 1).reshape(n, -1)
        opacity = p["opacities"].detach().cpu().numpy().reshape(n, 1)
        scales = p["scales"].detach().cpu().numpy()
        quats = p["quats"].detach().cpu().numpy()
        cols = ["x", "y", "z"]
        cols += [f"f_dc_{i}" for i in range(f_dc.shape[1])]
        cols += [f"f_rest_{i}" for i in range(f_rest.shape[1])]
        cols += ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
        header = ("ply\nformat binary_little_endian 1.0\n"
                  f"element vertex {n}\n" + "".join(f"property float {c}\n" for c in cols)
                  + "end_header\n").encode("ascii")
        arr = np.concatenate([means, f_dc, f_rest, opacity, scales, quats], axis=1).astype("<f4")
        with open(path, "wb") as f:
            f.write(header)
            f.write(arr.tobytes())


def _ssim(pred: torch.Tensor, gt: torch.Tensor) -> torch.Tensor:
    # 11x11 gaussian-window SSIM on (B,H,W,3). Small self-contained impl to avoid deps.
    pred = pred.permute(0, 3, 1, 2)
    gt = gt.permute(0, 3, 1, 2)
    win = _gaussian_window(11, 1.5, pred.shape[1]).to(pred)
    pad = 5
    mu1 = F.conv2d(pred, win, padding=pad, groups=pred.shape[1])
    mu2 = F.conv2d(gt, win, padding=pad, groups=gt.shape[1])
    mu1_sq, mu2_sq, mu12 = mu1 * mu1, mu2 * mu2, mu1 * mu2
    s1 = F.conv2d(pred * pred, win, padding=pad, groups=pred.shape[1]) - mu1_sq
    s2 = F.conv2d(gt * gt, win, padding=pad, groups=gt.shape[1]) - mu2_sq
    s12 = F.conv2d(pred * gt, win, padding=pad, groups=pred.shape[1]) - mu12
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    ssim_map = ((2 * mu12 + c1) * (2 * s12 + c2)) / ((mu1_sq + mu2_sq + c1) * (s1 + s2 + c2))
    return ssim_map.mean()


def _gaussian_window(size: int, sigma: float, channels: int) -> torch.Tensor:
    coords = torch.arange(size, dtype=torch.float32) - size // 2
    g = torch.exp(-(coords ** 2) / (2 * sigma ** 2))
    g = (g / g.sum())
    w = (g[:, None] @ g[None, :])
    return w.expand(channels, 1, size, size).contiguous()
