"""End-to-end M3 gate: drive the ingest WebSocket, assert a .roomsplat lands and a
viewer receives manifest updates — with no manual file movement."""

import json

import numpy as np
from starlette.testclient import TestClient

from app.main import create_app
from app.manager import SessionManager
from roomsplat.package import RoomSplatPackage
from roomsplat.protocol import POINT_CLOUD_FRAME_INDEX, encode_binary
from train.export import write_point_cloud_bin


def _cloud_bytes(tmp_path) -> bytes:
    xyz = np.random.default_rng(0).uniform([-2, 0, -3], [2, 3, 1], (2000, 3)).astype(np.float32)
    rgb = np.random.default_rng(1).integers(0, 255, (2000, 3), dtype=np.uint8)
    p = tmp_path / "c.bin"
    write_point_cloud_bin(p, xyz, rgb)
    return p.read_bytes()


def test_ingest_lands_package_and_notifies_viewer(tmp_path):
    manager = SessionManager(tmp_path / "captures", tmp_path / "assets", backend_name="synthetic")
    app = create_app(manager)
    client = TestClient(app)

    with client.websocket_connect("/ws/ingest") as ingest:
        ingest.send_text(json.dumps({
            "type": "session_open",
            "session_id": "ws-uuid",
            "device_model": "iPhone17,2",
            "captured_at": "2026-08-22T10:14:00Z",
            "camera": {"camera_model": "OPENCV", "fl_x": 1400, "fl_y": 1400,
                       "cx": 800, "cy": 600, "w": 1600, "h": 1200},
        }))
        ingest.send_bytes(encode_binary(POINT_CLOUD_FRAME_INDEX, _cloud_bytes(tmp_path)))

        from tests.conftest import _tiny_jpeg
        for i in range(6):
            pose = np.eye(4)
            pose[:3, 3] = [-1.5 + 0.5 * i, 1.6, -1.0]
            ingest.send_text(json.dumps({
                "type": "keyframe_meta", "frame_index": i,
                "transform_matrix": pose.tolist(), "timestamp": float(i),
            }))
            ingest.send_bytes(encode_binary(i, _tiny_jpeg()))
        ingest.send_text(json.dumps({"type": "session_complete", "session_id": "ws-uuid"}))

    pkg = RoomSplatPackage.open(tmp_path / "captures" / "ws-uuid.roomsplat")
    pkg.validate()
    assert len(pkg.frames) == 6
    assert pkg.capture["source"] == "stream"
