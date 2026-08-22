"""Spatial chunking into a world-space voxel grid (SPEC.md §4).

The scene is partitioned into a `cell_size` (default 1 m³) grid using the LiDAR
cloud's bounds. Each occupied cell is trained/exported/delivered independently as
its own SPZ. The cell is the unit of change: Gaussian sets are unordered and splat
indices are unstable across densification/pruning, so we never do per-splat deltas.

Cell ids are `c_<ix>_<iy>_<iz>` where the indices are integer grid coordinates
(may be negative), matching the manifest example `c_12_3_-4`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DEFAULT_CELL_SIZE_M = 1.0


def cell_index(point: np.ndarray, cell_size: float) -> tuple[int, int, int]:
    ix, iy, iz = np.floor(np.asarray(point, dtype=np.float64) / cell_size).astype(np.int64)
    return int(ix), int(iy), int(iz)


def cell_id(index: tuple[int, int, int]) -> str:
    return f"c_{index[0]}_{index[1]}_{index[2]}"


def cell_bounds(index: tuple[int, int, int], cell_size: float) -> list[float]:
    ix, iy, iz = index
    return [
        ix * cell_size,
        iy * cell_size,
        iz * cell_size,
        (ix + 1) * cell_size,
        (iy + 1) * cell_size,
        (iz + 1) * cell_size,
    ]


@dataclass
class Cell:
    index: tuple[int, int, int]
    version: int = 0
    splats: int = 0
    dirty_score: float = 0.0
    # opaque model handle for the trainer (e.g. row mask into the global params)
    member_mask: np.ndarray | None = None

    @property
    def id(self) -> str:
        return cell_id(self.index)


@dataclass
class Chunker:
    """Owns the occupied cell set and per-cell dirty accounting."""

    cell_size: float = DEFAULT_CELL_SIZE_M
    cells: dict[tuple[int, int, int], Cell] = field(default_factory=dict)

    def assign(self, points: np.ndarray) -> np.ndarray:
        """Map each point (N,3) to its cell index, creating occupied cells.

        Returns an (N,) array of flat cell keys so callers can group splats by cell.
        """
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        idx = np.floor(points / self.cell_size).astype(np.int64)
        keys = [tuple(row) for row in idx]
        for k in keys:
            if k not in self.cells:
                self.cells[k] = Cell(index=k)
        return idx

    def bump_dirty(self, index: tuple[int, int, int], delta: float, splats: int) -> None:
        cell = self.cells.setdefault(index, Cell(index=index))
        cell.splats = splats
        # dirty score is the sum of parameter deltas normalized by splat count (§4).
        cell.dirty_score += delta / max(splats, 1)

    def select_for_export(self, budget_bytes: int, bytes_per_splat: int = 30) -> list[Cell]:
        """Top-N dirtiest cells whose combined payload stays under the tick budget.

        Cells that have converged (dirty_score == 0) are never re-sent, which is what
        keeps bandwidth sustainable over a long walkthrough.
        """
        dirty = [c for c in self.cells.values() if c.dirty_score > 0 and c.splats > 0]
        dirty.sort(key=lambda c: c.dirty_score, reverse=True)
        selected: list[Cell] = []
        spent = 0
        for cell in dirty:
            cost = cell.splats * bytes_per_splat
            if selected and spent + cost > budget_bytes:
                break
            selected.append(cell)
            spent += cost
        return selected

    def mark_exported(self, cell: Cell) -> None:
        cell.version += 1
        cell.dirty_score = 0.0
