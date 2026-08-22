"""Server-side live session: frame receiver, session store, disk mirror.

A LiveSession is transport-agnostic — the FastAPI WebSocket handler (app/main.py)
and the --replay driver (app/replay.py) both drive it through the same methods, so
a replay reproduces server-side state exactly (SPEC.md §3 gate, §4 invariants).

Invariant (SPEC.md §4): every streamed session is also a file on disk, in the same
`.roomsplat` format as debug mode. There is no flag to disable it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from roomsplat.package import CameraModel, RoomSplatPackage
from roomsplat.protocol import KeyframeMeta

log = logging.getLogger("roomsplat.ingest")


@dataclass
class SessionStats:
    frame_count: int = 0
    frames_dropped: int = 0
    backpressure_events: int = 0
    thermal_events: list[dict[str, Any]] = field(default_factory=list)
    tracking_warnings: list[str] = field(default_factory=list)


class LiveSession:
    """Owns the disk mirror and in-memory state for one ingest session."""

    def __init__(self, session_id: str, root: Path, capture_meta: dict[str, Any], camera: CameraModel):
        self.session_id = session_id
        self.root = Path(root)
        self.package = RoomSplatPackage.create(self.root, capture_meta, camera)
        self.stats = SessionStats()
        self.latest_pose: list[list[float]] | None = None
        self.point_cloud_version = 0
        self.point_cloud: tuple[np.ndarray, np.ndarray] | None = None
        # frame_index -> pose, populated by keyframe_meta and consumed by the image
        self._pending_meta: dict[int, KeyframeMeta] = {}
        self._written: set[int] = set()
        self._highest_ack = -1
        self._complete = False

    # ---- control messages ---------------------------------------------------

    def on_keyframe_meta(self, meta: KeyframeMeta) -> None:
        """A keyframe_meta text frame arrived; stash the pose until its image lands."""
        self._pending_meta[meta.frame_index] = meta
        self.latest_pose = meta.transform_matrix

    def on_thermal_state(self, t: float, state: str) -> None:
        self.stats.thermal_events.append({"t": t, "state": state})
        self.package.update_capture(thermal_events=self.stats.thermal_events)

    def on_backpressure(self) -> None:
        self.stats.backpressure_events += 1
        self.package.update_capture(backpressure_events=self.stats.backpressure_events)

    def on_tracking_warning(self, message: str) -> None:
        self.stats.tracking_warnings.append(message)
        self.package.update_capture(tracking_warnings=self.stats.tracking_warnings)

    # ---- binary frames ------------------------------------------------------

    def on_image(self, frame_index: int, jpeg: bytes) -> str | None:
        """Pair an image with its previously-received pose and mirror to disk.

        Returns the written file_path, or None if the pose never arrived (a dropped
        keyframe_meta — logged, not fatal; the stream is lossy by design, §4).

        Idempotent: a frame_index already mirrored (e.g. re-sent after a reconnect) is
        skipped, so resume never duplicates or double-counts (§4).
        """
        if frame_index in self._written:
            return None
        meta = self._pending_meta.pop(frame_index, None)
        if meta is None:
            self.stats.frames_dropped += 1
            log.warning("image %d arrived without keyframe_meta; dropping", frame_index)
            self.package.update_capture(frames_dropped=self.stats.frames_dropped)
            return None
        file_path = self.package.append_keyframe(frame_index, jpeg, meta.transform_matrix)
        self._written.add(frame_index)
        self.stats.frame_count += 1
        self._highest_ack = max(self._highest_ack, frame_index)
        self.package.update_capture(frame_count=self.stats.frame_count)
        return file_path

    def on_point_cloud(self, xyz: np.ndarray, rgb: np.ndarray) -> int:
        """Store and mirror the fused LiDAR cloud; bump its version (§3, resent every 50 kf)."""
        self.point_cloud = (xyz, rgb)
        self.package.write_point_cloud(xyz, rgb)
        self.point_cloud_version += 1
        self.package.update_capture(seed_point_count=int(len(xyz)))
        return self.point_cloud_version

    # ---- lifecycle ----------------------------------------------------------

    @property
    def highest_ack(self) -> int:
        """Highest frame_index durably mirrored; the client resumes from here (§4)."""
        return self._highest_ack

    def complete(self) -> None:
        self._complete = True
        self.package.update_capture(frame_count=self.stats.frame_count)
        self.package.validate()

    @property
    def is_complete(self) -> bool:
        return self._complete
