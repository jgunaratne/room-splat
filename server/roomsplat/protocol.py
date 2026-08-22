"""The single-WebSocket ingest protocol (SPEC.md §3).

One WebSocket carries both control and payload so there is only one ordering to
reason about. Text frames are JSON control messages; binary frames are
`[4-byte big-endian frame_index][JPEG bytes]` (or the fused LiDAR cloud).

Each image binary frame is preceded by its `keyframe_meta` text frame carrying the
pose/intrinsics/timestamp, so the receiver pairs meta -> image by arrival order and
by frame_index. If they disagree it is the smeared-blob bug in SPEC.md §5.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Any

FRAME_INDEX_STRUCT = struct.Struct(">I")  # 4-byte big-endian


class ControlType(str, Enum):
    SESSION_OPEN = "session_open"
    KEYFRAME_META = "keyframe_meta"
    CAPABILITY_REPORT = "capability_report"
    THERMAL_STATE = "thermal_state"
    TRACKING_WARNING = "tracking_warning"
    SESSION_COMPLETE = "session_complete"
    SESSION_ABORT = "session_abort"
    # server -> phone
    ACK = "ack"
    STAGE_ASSIGNMENT = "stage_assignment"


# frame_index sentinel for the fused LiDAR cloud binary frame (not an image).
POINT_CLOUD_FRAME_INDEX = 0xFFFFFFFF


def encode_binary(frame_index: int, payload: bytes) -> bytes:
    return FRAME_INDEX_STRUCT.pack(frame_index) + payload


def decode_binary(data: bytes) -> tuple[int, bytes]:
    if len(data) < FRAME_INDEX_STRUCT.size:
        raise ValueError("binary frame shorter than 4-byte header")
    (index,) = FRAME_INDEX_STRUCT.unpack(data[: FRAME_INDEX_STRUCT.size])
    return index, data[FRAME_INDEX_STRUCT.size :]


def is_point_cloud(frame_index: int) -> bool:
    return frame_index == POINT_CLOUD_FRAME_INDEX


@dataclass
class KeyframeMeta:
    """Payload of a `keyframe_meta` control message."""

    frame_index: int
    transform_matrix: list[list[float]]  # 4x4 camera-to-world, Nerfstudio convention
    timestamp: float

    @classmethod
    def from_msg(cls, msg: dict[str, Any]) -> "KeyframeMeta":
        return cls(
            frame_index=int(msg["frame_index"]),
            transform_matrix=[[float(x) for x in row] for row in msg["transform_matrix"]],
            timestamp=float(msg.get("timestamp", 0.0)),
        )
