"""Training backends behind a single interface (SPEC.md M6: --backend {gsplat,fastgs,fastergs}).

The interface is deliberately tiny: `step(n_iters)` advances training and `cloud()`
returns the current GaussianCloud for chunking/export. The progressive trainer drives
it on a wall-clock cadence and never restarts on new keyframes (§4).

- SyntheticBackend: GPU-free, deterministic. Seeds Gaussians from the LiDAR cloud and
  perturbs them toward convergence so the full export/manifest/viewer path is testable
  in CI without torch (the M2/M4 correctness gates run against real gsplat on the 5090).
- GsplatBackend: real training. Lazily imports torch/gsplat so importing this module
  never requires CUDA.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from .gaussian import GaussianCloud


class Backend(Protocol):
    def step(self, n_iters: int) -> None: ...
    def cloud(self) -> GaussianCloud: ...
    @property
    def total_iters(self) -> int: ...


class SyntheticBackend:
    """Deterministic, GPU-free stand-in used for tests and --replay smoke runs."""

    def __init__(self, seed_xyz: np.ndarray, seed_rgb: np.ndarray, seed: int = 0):
        self._cloud = GaussianCloud.from_seed_points(seed_xyz, seed_rgb)
        self._iters = 0
        self._rng = np.random.default_rng(seed)
        self._target_opacity = np.full_like(self._cloud.opacity, 0.9)

    def step(self, n_iters: int) -> None:
        # Move opacity toward its target and jitter means slightly, so per-cell
        # dirty scores are non-zero early and decay to zero as cells converge.
        for _ in range(max(1, n_iters // 100)):
            self._cloud.opacity += 0.05 * (self._target_opacity - self._cloud.opacity)
        self._iters += n_iters

    def cloud(self) -> GaussianCloud:
        return self._cloud

    @property
    def total_iters(self) -> int:
        return self._iters


class GsplatBackend:
    """Real gsplat training. Structure only; requires torch+gsplat on the 5090 box.

    Reads a `.roomsplat` package through the shared parser, builds a gsplat trainer
    seeded from points3D.ply, and exposes the current Gaussians as a GaussianCloud.
    """

    def __init__(self, package_root, backend: str = "gsplat"):
        import torch  # noqa: F401  (lazy: never import CUDA at module load)

        try:
            import gsplat  # noqa: F401
        except ImportError as e:  # pragma: no cover - depends on the box
            raise RuntimeError(
                "gsplat is not installed. Pin torch+CUDA+gsplat in pyproject.toml and "
                "record the working combination in server/README.md (SPEC.md §7)."
            ) from e
        self._backend = backend
        self._package_root = package_root
        self._iters = 0
        # Full trainer wiring (dataset, strategy, optimizers) is built on the GPU box.
        raise NotImplementedError(
            "GsplatBackend requires the 5090 environment; run the M2 gate there. "
            "Use SyntheticBackend for GPU-free pipeline tests."
        )

    def step(self, n_iters: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def cloud(self) -> GaussianCloud:  # pragma: no cover
        raise NotImplementedError

    @property
    def total_iters(self) -> int:  # pragma: no cover
        return self._iters


def make_backend(name: str, seed_xyz=None, seed_rgb=None, package_root=None) -> Backend:
    name = name.lower()
    if name == "synthetic":
        return SyntheticBackend(seed_xyz, seed_rgb)
    if name in ("gsplat", "fastgs", "fastergs"):
        return GsplatBackend(package_root, backend=name)
    raise ValueError(f"unknown backend {name!r}")
