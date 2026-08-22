"""Session manifest and diff protocol (SPEC.md §4).

The server maintains a manifest and pushes diffs over /ws/viewer. Cell URLs are
versioned and immutable, so the browser fetches them over plain HTTP; the WS carries
only notification. Diffs carry *absolute* cell versions (not increments) so they are
idempotent and order-independent: the viewer applies only versions newer than what
it holds, and a missed or out-of-order diff is self-correcting on the next tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chunking import Cell, cell_bounds


@dataclass
class CellEntry:
    id: str
    url: str
    version: int
    bounds: list[float]
    splats: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "version": self.version,
            "bounds": self.bounds,
            "splats": self.splats,
        }


@dataclass
class Manifest:
    session_id: str
    cell_size: float = 1.0
    asset_ext: str = "spz"
    tick: int = 0
    cells: dict[str, CellEntry] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    point_cloud_url: str | None = None
    live_pose: list[list[float]] | None = None
    coverage: dict[str, int] = field(default_factory=dict)

    def asset_prefix(self) -> str:
        return f"/assets/{self.session_id}"

    def apply_cell(self, cell: Cell) -> CellEntry:
        """Record a freshly exported cell version and return its immutable entry."""
        url = f"{self.asset_prefix()}/cells/{cell.id}.v{cell.version}.{self.asset_ext}"
        entry = CellEntry(
            id=cell.id,
            url=url,
            version=cell.version,
            bounds=cell_bounds(cell.index, self.cell_size),
            splats=cell.splats,
        )
        self.cells[cell.id] = entry
        return entry

    def set_point_cloud(self, version: int) -> str:
        self.point_cloud_url = f"{self.asset_prefix()}/cloud.v{version}.bin"
        return self.point_cloud_url

    def snapshot(self) -> dict[str, Any]:
        """Full current state, for a late-joining browser (§4)."""
        return {
            "type": "manifest_update",
            "session_id": self.session_id,
            "tick": self.tick,
            "cells": [c.to_dict() for c in self.cells.values()],
            "removed": [],
            "point_cloud_url": self.point_cloud_url,
            "live_pose": self.live_pose,
            "coverage": self.coverage,
        }

    def diff(self, changed: list[CellEntry], removed: list[str] | None = None) -> dict[str, Any]:
        """A diff carrying absolute versions for just the changed cells."""
        self.tick += 1
        return {
            "type": "manifest_update",
            "session_id": self.session_id,
            "tick": self.tick,
            "cells": [c.to_dict() for c in changed],
            "removed": removed or [],
            "point_cloud_url": self.point_cloud_url,
            "live_pose": self.live_pose,
            "coverage": self.coverage,
        }
