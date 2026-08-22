import json

import numpy as np
import pytest

from roomsplat.package import CameraModel, PackageError, RoomSplatPackage, read_ply, write_ply


def test_roundtrip_and_validate(make_package):
    pkg = make_package(n_frames=6, n_points=1000)
    reopened = RoomSplatPackage.open(pkg.root)
    reopened.validate()
    assert len(reopened.frames) == 6
    assert reopened.camera.w == 1600
    xyz, rgb = reopened.read_point_cloud()
    assert xyz.shape == (1000, 3) and rgb.shape == (1000, 3)


def test_transforms_json_always_valid_after_append(make_package):
    pkg = make_package(n_frames=3)
    # transforms.json must be parseable and 4x4 after every append (§6).
    doc = json.loads((pkg.root / "transforms.json").read_text())
    assert doc["camera_model"] == "OPENCV"
    for f in doc["frames"]:
        assert len(f["transform_matrix"]) == 4
        assert all(len(r) == 4 for r in f["transform_matrix"])


def test_rejects_unknown_schema_version(tmp_path):
    cam = CameraModel("OPENCV", 1.0, 1.0, 0.0, 0.0, 2, 2)
    with pytest.raises(PackageError):
        RoomSplatPackage.create(tmp_path / "x.roomsplat",
                                {"schema_version": 99, "session_id": "s",
                                 "device_model": "d", "captured_at": "t", "source": "debug"}, cam)


def test_ply_binary_roundtrip(tmp_path):
    xyz = np.random.default_rng(1).uniform(-1, 1, (500, 3)).astype(np.float32)
    rgb = np.random.default_rng(2).integers(0, 255, (500, 3), dtype=np.uint8)
    write_ply(tmp_path / "p.ply", xyz, rgb)
    rx, rrgb = read_ply(tmp_path / "p.ply")
    assert np.allclose(rx, xyz, atol=1e-5)
    assert np.array_equal(rrgb, rgb)
