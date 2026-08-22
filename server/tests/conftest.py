import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roomsplat.package import CameraModel, RoomSplatPackage  # noqa: E402


def _tiny_jpeg() -> bytes:
    # Smallest valid-ish JPEG payload; the pipeline treats images as opaque bytes.
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        "080606070605080707070909080a0c140d0c0b0b0c1912130f14"
        "1d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434"
        "1f27393d38323c2e333432ffc0000b080001000101011100ffc4"
        "001f0000010501010101010100000000000000000102030405060708090a0b"
        "ffc400b5100002010303020403050504040000017d01020300041105122131"
        "410613516107227114328191a1082342b1c11552d1f02433627282090a1617"
        "18191a25262728292a3435363738393a434445464748494a535455565758595a"
        "636465666768696a737475767778797a838485868788898a92939495969798999a"
        "a2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9da"
        "e1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfeffd9"
    )


@pytest.fixture
def make_package(tmp_path):
    def _make(session_id="test-uuid", n_frames=8, n_points=2000, source="debug"):
        camera = CameraModel(camera_model="OPENCV", fl_x=1400.0, fl_y=1400.0,
                             cx=800.0, cy=600.0, w=1600, h=1200)
        capture = {
            "schema_version": 2,
            "session_id": session_id,
            "device_model": "iPhone17,2",
            "captured_at": "2026-08-22T10:14:00Z",
            "source": source,
        }
        root = tmp_path / f"{session_id}.roomsplat"
        pkg = RoomSplatPackage.create(root, capture, camera)

        rng = np.random.default_rng(0)
        # A little box-shaped room spanning a few cells.
        xyz = rng.uniform([-2, 0, -3], [2, 3, 1], size=(n_points, 3)).astype(np.float32)
        rgb = rng.integers(0, 255, size=(n_points, 3), dtype=np.uint8)
        pkg.write_point_cloud(xyz, rgb)

        for i in range(n_frames):
            t = i / max(1, n_frames - 1)
            pose = np.eye(4, dtype=np.float32)
            pose[:3, 3] = [(-1.5 + 3 * t), 1.6, -1.0]  # walk along +x
            pkg.append_keyframe(i, _tiny_jpeg(), [[float(v) for v in row] for row in pose])
        pkg.update_capture(frame_count=n_frames, seed_point_count=n_points)
        pkg.validate()
        return pkg

    return _make
