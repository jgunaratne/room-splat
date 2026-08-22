"""Progressive training loop: wall-clock export, dirty-cell selection, coverage.

Drives a Backend on a 2 s wall-clock cadence (SPEC.md §4: "Update cadence is
wall-clock, not iteration count"), assigns the current Gaussians to the voxel grid,
scores per-cell change, exports the dirtiest cells under a per-tick byte budget, and
produces manifest diffs. New keyframes are added to the training set as they land;
training is never restarted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from roomsplat.chunking import Chunker
from roomsplat.manifest import CellEntry, Manifest

from .backends import Backend
from .export import export_cell_ply, write_point_cloud_bin

log = logging.getLogger("roomsplat.train")

TICK_SECONDS = 2.0
EXPORT_BUDGET_BYTES = 1_500_000  # ~1.5 MB/tick (§4)
RECENT_WINDOW = 20
RECENT_FRACTION = 0.7
COVERAGE_MAX_DEPTH = 5.0


def sample_view_indices(n_frames: int, batch: int, rng: np.random.Generator) -> np.ndarray:
    """70% from the most recent RECENT_WINDOW keyframes, 30% uniform (SPEC.md §4)."""
    if n_frames == 0:
        return np.empty(0, dtype=int)
    n_recent = int(round(batch * RECENT_FRACTION))
    recent_lo = max(0, n_frames - RECENT_WINDOW)
    recent = rng.integers(recent_lo, n_frames, size=n_recent)
    uniform = rng.integers(0, n_frames, size=batch - n_recent)
    return np.concatenate([recent, uniform])


@dataclass
class ProgressiveTrainer:
    backend: Backend
    manifest: Manifest
    assets_dir: Path
    cell_size: float = 1.0
    chunker: Chunker = field(init=False)
    _cam_centers: list[np.ndarray] = field(default_factory=list)
    _cam_forwards: list[np.ndarray] = field(default_factory=list)
    _last_means: np.ndarray | None = None
    _last_tick: float = 0.0

    def __post_init__(self):
        self.chunker = Chunker(cell_size=self.cell_size)
        self.manifest.asset_ext = "ply"  # GPU-free path exports PLY; SPZ on the 5090
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def add_keyframe(self, transform_matrix: list[list[float]]) -> None:
        m = np.asarray(transform_matrix, dtype=np.float64).reshape(4, 4)
        self._cam_centers.append(m[:3, 3])
        self._cam_forwards.append(-m[:3, 2])  # camera looks -Z (Nerfstudio)
        self.manifest.live_pose = transform_matrix

    def set_point_cloud(self, xyz: np.ndarray, rgb: np.ndarray, version: int) -> None:
        url = self.manifest.set_point_cloud(version)
        name = url.rsplit("/", 1)[-1]
        write_point_cloud_bin(self.assets_dir / name, xyz, rgb)

    def _score_dirty(self) -> None:
        cloud = self.backend.cloud()
        means = cloud.means
        idx = self.chunker.assign(means)
        keys = [tuple(row) for row in idx]
        if self._last_means is not None and len(self._last_means) == len(means):
            delta = np.linalg.norm(means - self._last_means, axis=1)
        else:
            delta = np.full(len(means), 1.0)  # first tick: everything is dirty
        # accumulate per-cell delta sums and splat counts
        sums: dict[tuple, float] = {}
        counts: dict[tuple, int] = {}
        for k, d in zip(keys, delta):
            sums[k] = sums.get(k, 0.0) + float(d)
            counts[k] = counts.get(k, 0) + 1
        for k, s in sums.items():
            self.chunker.bump_dirty(k, s, counts[k])
        self._last_means = means.copy()

    def _coverage(self) -> dict[str, int]:
        if not self._cam_centers:
            return {}
        centers = np.array(self._cam_centers)
        forwards = np.array(self._cam_forwards)
        cov: dict[str, int] = {}
        for cell in self.chunker.cells.values():
            b = np.array([(cell.index[i] + 0.5) * self.cell_size for i in range(3)])
            to_cell = b - centers
            dist = np.linalg.norm(to_cell, axis=1)
            in_range = dist <= COVERAGE_MAX_DEPTH
            in_front = np.einsum("ij,ij->i", to_cell, forwards) > 0
            cov[cell.id] = int(np.count_nonzero(in_range & in_front))
        return cov

    def _export(self, cells, stage: str) -> dict:
        """Export the given cells at the current model state; return a manifest diff."""
        t = time.monotonic()
        cloud = self.backend.cloud()
        idx = self.chunker.assign(cloud.means)
        keys = np.array([f"{a}_{b}_{c}" for a, b, c in idx])
        changed: list[CellEntry] = []
        exported_bytes = 0
        for cell in cells:
            mask = keys == f"{cell.index[0]}_{cell.index[1]}_{cell.index[2]}"
            cell.splats = int(np.count_nonzero(mask))
            if cell.splats == 0:
                continue
            self.chunker.mark_exported(cell)
            entry = self.manifest.apply_cell(cell)
            name = entry.url.rsplit("/", 1)[-1]
            exported_bytes += export_cell_ply(self.assets_dir / "cells" / name, cloud.subset(mask))
            changed.append(entry)
        log.info(
            "stage=%s session=%s tick=%d cells=%d bytes=%d wall_s=%.2f",
            stage, self.manifest.session_id, self.manifest.tick + 1, len(changed),
            exported_bytes, time.monotonic() - t,
        )
        return self.manifest.diff(changed)

    def tick(self, iters: int = 200) -> dict:
        """Advance training and export the dirtiest cells under budget (SPEC.md §4).

        Emits one structured line per stage with wall-clock duration (SPEC.md §8): you
        cannot optimize M6 without this history.
        """
        t = time.monotonic()
        self.backend.step(iters)
        log.info("stage=train session=%s iters=%d splats=%d wall_s=%.2f",
                 self.manifest.session_id, iters, len(self.backend.cloud()), time.monotonic() - t)
        self._score_dirty()
        self.manifest.coverage = self._coverage()
        self._last_tick = time.monotonic()
        return self._export(self.chunker.select_for_export(EXPORT_BUDGET_BYTES), stage="export")

    def finish(self, iters: int, max_seconds: float = 60.0) -> dict:
        """Finishing pass on session end (SPEC.md M6): train much longer, then re-export
        EVERY occupied cell at final quality (ignoring the per-tick budget). The live
        walkthrough only accumulates a few hundred iterations per region, so this is what
        turns the preview into the deliverable.

        Bounded by a wall-clock budget as well as an iteration target, so it can't run
        for minutes on full-resolution frames and blow the M6 latency target.
        """
        t = time.monotonic()
        done = 0
        batch = 200
        while done < iters and (time.monotonic() - t) < max_seconds:
            self.backend.step(min(batch, iters - done))
            done += batch
        log.info("stage=finish_train session=%s iters=%d splats=%d wall_s=%.1f",
                 self.manifest.session_id, done, len(self.backend.cloud()), time.monotonic() - t)
        self._score_dirty()
        self.manifest.coverage = self._coverage()
        return self._export(list(self.chunker.cells.values()), stage="finish_export")
