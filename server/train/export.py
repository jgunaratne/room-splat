"""Cell and point-cloud asset export for the viewer.

Point cloud wire format (cloud.v<n>.bin), consumed by web/src/pointcloud.js:
    [uint32 count][count x (float32 x, float32 y, float32 z, uint8 r, g, b)]
little-endian, 15 bytes per point. Deliberately trivial so the geometry layer has
sub-500ms latency (SPEC.md §4).

Cells are exported as 3DGS PLY by the GPU-free path; on the 5090 box the same call
site converts to SPZ (SPEC.md M6) and the manifest's asset_ext switches to "spz".
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from .gaussian import GaussianCloud


def write_point_cloud_bin(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    xyz = np.asarray(xyz, dtype="<f4").reshape(-1, 3)
    rgb = np.asarray(rgb, dtype="u1").reshape(-1, 3)
    n = len(xyz)
    dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    packed = np.empty(n, dtype=dtype)
    packed["x"], packed["y"], packed["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    packed["r"], packed["g"], packed["b"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<I", n))
        f.write(packed.tobytes())


def read_point_cloud_bin(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    (n,) = struct.unpack_from("<I", data, 0)
    dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    rec = np.frombuffer(data, dtype=dtype, count=n, offset=4)
    xyz = np.stack([rec["x"], rec["y"], rec["z"]], axis=1).astype(np.float32)
    rgb = np.stack([rec["r"], rec["g"], rec["b"]], axis=1).astype(np.uint8)
    return xyz, rgb


def export_cell_ply(path: Path, cloud: GaussianCloud) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    cloud.write_ply(path)
    return path.stat().st_size
