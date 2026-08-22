"""Reconnect resumes, never restarts (SPEC.md §4, M3 hardening).

Drops the ingest socket mid-stream, reconnects with the same session_id, reads the
server's ack to find the resume point, and re-sends from there — asserting the disk
mirror has exactly the frames sent once, with no duplicates or gaps.
"""

import json

import numpy as np
from starlette.testclient import TestClient

from app.main import create_app
from app.manager import SessionManager
from roomsplat.package import RoomSplatPackage
from roomsplat.protocol import POINT_CLOUD_FRAME_INDEX, encode_binary
from tests.conftest import _tiny_jpeg
from train.export import write_point_cloud_bin


def _cloud(tmp_path) -> bytes:
    xyz = np.random.default_rng(0).uniform([-2, 0, -3], [2, 3, 1], (500, 3)).astype(np.float32)
    rgb = np.random.default_rng(1).integers(0, 255, (500, 3), dtype=np.uint8)
    p = tmp_path / "c.bin"
    write_point_cloud_bin(p, xyz, rgb)
    return p.read_bytes()


def _open(ws, cloud):
    ws.send_text(json.dumps({
        "type": "session_open", "session_id": "resume-uuid", "device_model": "d",
        "captured_at": "t",
        "camera": {"camera_model": "OPENCV", "fl_x": 1400, "fl_y": 1400,
                   "cx": 800, "cy": 600, "w": 1600, "h": 1200},
    }))
    ack = json.loads(ws.receive_text())
    assert ack["type"] == "ack"
    ws.send_bytes(encode_binary(POINT_CLOUD_FRAME_INDEX, cloud))
    return ack["frame_index"]


def _send_frame(ws, i):
    pose = np.eye(4)
    pose[:3, 3] = [i * 0.1, 1.6, -1.0]
    ws.send_text(json.dumps({"type": "keyframe_meta", "frame_index": i,
                             "transform_matrix": pose.tolist(), "timestamp": float(i)}))
    ws.send_bytes(encode_binary(i, _tiny_jpeg()))
    ack = json.loads(ws.receive_text())
    return ack["frame_index"]


def test_reconnect_resumes_without_gap_or_duplicate(tmp_path):
    manager = SessionManager(tmp_path / "captures", tmp_path / "assets", backend_name="synthetic")
    app = create_app(manager)
    client = TestClient(app)
    cloud = _cloud(tmp_path)

    # First connection: send frames 0..4, then "drop".
    with client.websocket_connect("/ws/ingest") as ws:
        assert _open(ws, cloud) == -1  # fresh session
        for i in range(5):
            assert _send_frame(ws, i) == i

    highest_before = manager.sessions["resume-uuid"].session.highest_ack
    assert highest_before == 4

    # Reconnect: server reports the resume point; client re-sends 4 (dup) then 5..9.
    with client.websocket_connect("/ws/ingest") as ws:
        resume_from = _open(ws, cloud)
        assert resume_from == 4, "server must report the highest durably-mirrored frame"
        _send_frame(ws, 4)          # duplicate of last acked frame — must be idempotent
        for i in range(5, 10):
            assert _send_frame(ws, i) == i
        ws.send_text(json.dumps({"type": "session_complete", "session_id": "resume-uuid"}))

    pkg = RoomSplatPackage.open(tmp_path / "captures" / "resume-uuid.roomsplat")
    pkg.validate()
    # Exactly 10 unique frames, no duplicate from the re-sent frame 4.
    assert len(pkg.frames) == 10
    names = [f.file_path for f in pkg.frames]
    assert len(set(names)) == 10
