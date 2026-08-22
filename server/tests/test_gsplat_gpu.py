"""GPU convergence gate for the gsplat backend (SPEC.md M2).

Skipped automatically when torch/gsplat/CUDA are absent, so the GPU-free suite is
unaffected. Renders ground-truth views from a known colored cloud, seeds training with
gray colors, and asserts the optimizer recovers appearance (loss drops sharply). If the
coordinate conversion in coords.py were mirrored/flipped, the seed geometry would not
line up with the ground-truth renders and the loss would not fall — so this also
exercises §5 end to end on real kernels.
"""

import io
import shutil
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("gsplat")
if not torch.cuda.is_available():
    pytest.skip("CUDA not available", allow_module_level=True)

from PIL import Image

from gsplat import rasterization
from roomsplat.coords import opengl_c2w_to_opencv_w2c
from roomsplat.package import CameraModel, RoomSplatPackage
from train.gaussian import rgb_to_sh_dc
from train.gsplat_trainer import GsplatTrainer


def _render_gt(xyz, rgb01, c2ws, K, W, H):
    dev = "cuda"
    means = torch.tensor(xyz, device=dev)
    n = len(xyz)
    quats = torch.zeros(n, 4, device=dev); quats[:, 0] = 1
    scales = torch.full((n, 3), 0.05, device=dev)
    opac = torch.full((n,), 0.9, device=dev)
    colors = torch.tensor(rgb_to_sh_dc(rgb01), device=dev)[:, None, :]  # DC-only SH
    viewmats = torch.stack([torch.tensor(opengl_c2w_to_opencv_w2c(c), dtype=torch.float32, device=dev)
                            for c in c2ws])
    Ks = torch.tensor(K, dtype=torch.float32, device=dev)[None].repeat(len(c2ws), 1, 1)
    out, _, _ = rasterization(means, quats, scales, opac, colors, viewmats, Ks, W, H, sh_degree=0)
    return out[..., :3].clamp(0, 1).detach().cpu().numpy()


def test_gsplat_recovers_appearance(tmp_path):
    W, H = 64, 48
    K = [[80.0, 0, W / 2], [0, 80.0, H / 2], [0, 0, 1]]
    rng = np.random.default_rng(0)
    xyz = rng.uniform([-1, -1, -3.5], [1, 1, -2.5], (1500, 3)).astype(np.float32)
    span = np.ptp(xyz, axis=0) + 1e-6
    true_rgb01 = ((xyz - xyz.min(0)) / span).astype(np.float32)

    c2ws = []
    for i in range(6):
        c = np.eye(4, dtype=np.float32); c[0, 3] = -0.3 + 0.12 * i
        c2ws.append(c)
    gt = _render_gt(xyz, true_rgb01, c2ws, K, W, H)

    root = tmp_path / "conv.roomsplat"
    shutil.rmtree(root, ignore_errors=True)
    cam = CameraModel("OPENCV", 80.0, 80.0, W / 2, H / 2, W, H)
    pkg = RoomSplatPackage.create(
        root, {"schema_version": 2, "session_id": "conv", "device_model": "t",
               "captured_at": "t", "source": "debug"}, cam)
    gray = np.full_like(true_rgb01, 0.5)  # seed with NO color information
    pkg.write_point_cloud(xyz, (gray * 255).astype(np.uint8))
    for i, c in enumerate(c2ws):
        buf = io.BytesIO()
        Image.fromarray((gt[i] * 255).astype(np.uint8)).save(buf, format="PNG")
        # store as jpg name per schema, PNG bytes decode fine
        pkg.append_keyframe(i, buf.getvalue(), c.tolist())
    pkg.update_capture(frame_count=len(c2ws), seed_point_count=len(xyz))
    pkg.validate()

    trainer = GsplatTrainer(root, sh_degree=0, max_image_long_edge=64)
    with torch.no_grad():
        pred0 = trainer._rasterize(torch.arange(len(c2ws), device="cuda"))[0][..., :3].clamp(0, 1)
    loss0 = torch.nn.functional.l1_loss(pred0, trainer.images).item()
    trainer.step(300)
    with torch.no_grad():
        pred1 = trainer._rasterize(torch.arange(len(c2ws), device="cuda"))[0][..., :3].clamp(0, 1)
    loss1 = torch.nn.functional.l1_loss(pred1, trainer.images).item()

    assert loss1 < loss0 * 0.5, f"training did not converge: {loss0:.4f} -> {loss1:.4f}"
