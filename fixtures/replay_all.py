#!/usr/bin/env python3
"""Replay all fixtures through the server pipeline and verify output.

This is the CI-facing script referenced by the GitHub Actions workflow.
It generates synthetic fixtures (if they don't already contain .roomsplat
directories), replays each through SessionManager with the synthetic backend,
and asserts the replay produced valid output.

Exit 0 = all fixtures replayed successfully. Non-zero = failure.

Usage:
    python fixtures/replay_all.py [--fixtures fixtures/] [--out /tmp/replay]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# The server package is one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.manager import SessionManager  # noqa: E402
from app.replay import replay_package  # noqa: E402
from roomsplat.package import RoomSplatPackage  # noqa: E402


log = logging.getLogger("roomsplat.ci")


def verify_replay(out_dir: Path, session_id: str, manager: SessionManager) -> list[str]:
    """Return a list of failure messages (empty = pass)."""
    errors: list[str] = []

    # 1. Disk mirror must be a valid .roomsplat
    mirror = out_dir / "captures" / f"{session_id}.roomsplat"
    if not mirror.is_dir():
        errors.append(f"disk mirror not found: {mirror}")
        return errors
    try:
        pkg = RoomSplatPackage.open(mirror)
        pkg.validate()
    except Exception as e:
        errors.append(f"disk mirror invalid: {e}")
        return errors

    # 2. Must have frames
    if len(pkg.frames) == 0:
        errors.append("disk mirror has 0 frames")

    # 3. Cell assets must be exported
    assets = out_dir / "assets" / session_id
    cells = list((assets / "cells").glob("*.ply")) if (assets / "cells").is_dir() else []
    if not cells:
        errors.append("no cell assets exported")

    # 4. Point cloud asset must exist
    clouds = list(assets.glob("cloud.v*.bin"))
    if not clouds:
        errors.append("no point cloud asset exported")

    # 5. Manifest snapshot must be self-consistent
    rt = manager.sessions.get(session_id)
    if rt is None:
        errors.append(f"session {session_id} not found in manager")
    else:
        snap = rt.manifest.snapshot()
        if snap.get("point_cloud_url") is None:
            errors.append("manifest snapshot has no point_cloud_url")
        if len(snap.get("cells", [])) == 0:
            errors.append("manifest snapshot has no cells")

    return errors


async def run(fixtures_dir: Path, out_dir: Path) -> int:
    packages = sorted(fixtures_dir.glob("*.roomsplat"))
    if len(packages) < 3:
        log.error("SPEC.md §8 requires ≥ 3 fixtures, found %d in %s", len(packages), fixtures_dir)
        return 1

    manager = SessionManager(out_dir / "captures", out_dir / "assets", backend_name="synthetic")
    failures: dict[str, list[str]] = {}

    for pkg_path in packages:
        name = pkg_path.stem.replace(".roomsplat", "")
        log.info("replaying %s …", name)
        t0 = time.monotonic()
        try:
            sid = await replay_package(pkg_path, manager, speed=0.0)
        except Exception as e:
            failures[name] = [f"replay crashed: {e}"]
            continue
        wall = time.monotonic() - t0
        log.info("  replayed %s in %.1fs", name, wall)

        errs = verify_replay(out_dir, sid, manager)
        if errs:
            failures[name] = errs

    # Summary
    print(f"\n{'='*60}")
    print(f"Replayed {len(packages)} fixture(s):")
    for pkg_path in packages:
        name = pkg_path.stem.replace(".roomsplat", "")
        if name in failures:
            print(f"  ✗ {name}")
            for e in failures[name]:
                print(f"      → {e}")
        else:
            print(f"  ✓ {name}")
    print(f"{'='*60}\n")

    if failures:
        log.error("%d fixture(s) FAILED", len(failures))
        return 1
    log.info("All %d fixture(s) passed", len(packages))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="CI replay verification")
    ap.add_argument("--fixtures", type=Path,
                    default=Path(__file__).resolve().parent,
                    help="Directory containing .roomsplat fixture dirs")
    ap.add_argument("--out", type=Path, default=Path("/tmp/roomsplat-ci"),
                    help="Replay output directory")
    args = ap.parse_args(argv)

    return asyncio.run(run(args.fixtures, args.out))


if __name__ == "__main__":
    sys.exit(main())
