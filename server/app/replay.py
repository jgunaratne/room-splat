"""Replay a recorded .roomsplat through the ingest path (SPEC.md M3 gate, §4).

Feeds a package's keyframes and seed cloud through the SAME SessionManager methods
the live WebSocket handler uses, so replay reproduces server-side state end-to-end.
Every performance claim in the project is measured against recorded input via this
path, never against a fresh walkthrough (§8).

    python -m app.replay --package data/captures/<uuid>.roomsplat --out /tmp/replayed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from ingest.session import LiveSession
from roomsplat.package import RoomSplatPackage
from roomsplat.protocol import KeyframeMeta

from .manager import SessionManager

log = logging.getLogger("roomsplat.replay")


async def replay_package(
    package_root: Path, manager: SessionManager, speed: float = 0.0, resend_every: int = 50
) -> str:
    pkg = RoomSplatPackage.open(package_root)
    pkg.validate()
    session_id = pkg.capture.get("session_id", package_root.stem.replace(".roomsplat", ""))

    open_msg = {
        "type": "session_open",
        "session_id": session_id,
        "device_model": pkg.capture.get("device_model", "unknown"),
        "captured_at": pkg.capture.get("captured_at", ""),
        "camera": pkg.camera.to_dict(),
        "capture": {**pkg.capture, "source": "stream", "session_id": session_id},
    }
    rt = await manager.open_session(open_msg)
    session: LiveSession = rt.session

    xyz, rgb = pkg.read_point_cloud()
    version = session.on_point_cloud(xyz, rgb)
    rt.on_point_cloud(xyz, rgb, version)

    for i, frame in enumerate(pkg.frames):
        meta = KeyframeMeta(frame_index=i, transform_matrix=frame.transform_matrix, timestamp=0.0)
        session.on_keyframe_meta(meta)
        rt.on_keyframe_pose(frame.transform_matrix)
        jpeg = (package_root / frame.file_path).read_bytes()
        if session.on_image(i, jpeg) is not None:
            rt.on_image()
        if i > 0 and i % resend_every == 0:
            version = session.on_point_cloud(xyz, rgb)
            rt.on_point_cloud(xyz, rgb, version)
        if speed > 0:
            await asyncio.sleep(0.2 / speed)  # ~5 keyframes/s at speed=1 (§3 budget)

    await manager.close_session(session_id)
    return session_id


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="RoomSplat replay")
    ap.add_argument("--package", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/replay"))
    ap.add_argument("--speed", type=float, default=0.0, help="0 = as fast as possible; 1 = capture speed")
    args = ap.parse_args(argv)

    manager = SessionManager(args.out / "captures", args.out / "assets")
    t0 = time.monotonic()
    sid = await replay_package(args.package, manager, speed=args.speed)
    log.info("stage=replay_done session=%s wall_s=%.1f", sid, time.monotonic() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
