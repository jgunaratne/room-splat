"""In-memory Gaussian set + 3DGS PLY I/O.

Kept in numpy so the whole export/chunking/manifest pipeline runs and is testable
without torch or a GPU. The gsplat backend converts to/from this representation at
tick boundaries. PLY layout is the standard 3DGS one (x y z, f_dc_0..2, opacity,
scale_0..2, rot_0..3) so Spark and every 3DGS tool reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

_SH_C0 = 0.28209479177387814  # DC term of the real SH basis


def rgb_to_sh_dc(rgb01: np.ndarray) -> np.ndarray:
    return (np.asarray(rgb01, dtype=np.float32) - 0.5) / _SH_C0


@dataclass
class GaussianCloud:
    means: np.ndarray  # (N,3) f32
    colors_dc: np.ndarray  # (N,3) f32, SH DC
    opacity: np.ndarray  # (N,1) f32, logit
    scales: np.ndarray  # (N,3) f32, log
    quats: np.ndarray  # (N,4) f32, wxyz

    def __len__(self) -> int:
        return len(self.means)

    @classmethod
    def from_seed_points(cls, xyz: np.ndarray, rgb: np.ndarray, init_scale: float = 0.02) -> "GaussianCloud":
        xyz = np.asarray(xyz, dtype=np.float32).reshape(-1, 3)
        rgb01 = np.asarray(rgb, dtype=np.float32).reshape(-1, 3) / 255.0
        n = len(xyz)
        return cls(
            means=xyz.copy(),
            colors_dc=rgb_to_sh_dc(rgb01),
            opacity=np.full((n, 1), 0.1, dtype=np.float32),
            scales=np.full((n, 3), np.log(init_scale), dtype=np.float32),
            quats=np.tile(np.array([1, 0, 0, 0], dtype=np.float32), (n, 1)),
        )

    def subset(self, mask: np.ndarray) -> "GaussianCloud":
        return GaussianCloud(
            means=self.means[mask],
            colors_dc=self.colors_dc[mask],
            opacity=self.opacity[mask],
            scales=self.scales[mask],
            quats=self.quats[mask],
        )

    def write_ply(self, path: Path) -> None:
        n = len(self)
        props = ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2", "opacity",
                 "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
        header = ("ply\nformat binary_little_endian 1.0\n"
                  f"element vertex {n}\n"
                  + "".join(f"property float {p}\n" for p in props)
                  + "end_header\n").encode("ascii")
        arr = np.concatenate(
            [self.means, self.colors_dc, self.opacity, self.scales, self.quats], axis=1
        ).astype("<f4")
        with open(path, "wb") as f:
            f.write(header)
            f.write(arr.tobytes())
