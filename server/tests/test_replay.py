import asyncio

from app.manager import SessionManager
from app.replay import replay_package
from roomsplat.package import RoomSplatPackage


def test_replay_reproduces_server_state(make_package, tmp_path):
    src = make_package(session_id="replay-uuid", n_frames=10, n_points=3000)

    manager = SessionManager(tmp_path / "captures", tmp_path / "assets", backend_name="synthetic")
    sid = asyncio.run(replay_package(src.root, manager, speed=0.0))
    assert sid == "replay-uuid"

    # Disk mirror is a valid .roomsplat, byte-identical in structure to debug output.
    mirrored = RoomSplatPackage.open(tmp_path / "captures" / "replay-uuid.roomsplat")
    mirrored.validate()
    assert len(mirrored.frames) == 10
    assert mirrored.capture["source"] == "stream"

    # Training produced versioned cell assets and a point-cloud asset.
    assets = tmp_path / "assets" / "replay-uuid"
    clouds = list(assets.glob("cloud.v*.bin"))
    cells = list((assets / "cells").glob("*.ply"))
    assert clouds, "point cloud asset was not exported"
    assert cells, "no cell assets exported"

    # Manifest snapshot is self-consistent for a late-joining viewer.
    rt = manager.sessions["replay-uuid"]
    snap = rt.manifest.snapshot()
    assert snap["point_cloud_url"] is not None
    assert snap["live_pose"] is not None
    assert len(snap["cells"]) == len(rt.manifest.cells)
