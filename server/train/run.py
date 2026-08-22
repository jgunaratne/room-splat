"""Offline training driver (SPEC.md M2 gate).

    python -m train.run --data <uuid>.roomsplat --init-ply points3D.ply \
        --backend gsplat --out /data/models/<uuid>/

M2 exists to isolate coordinate-system bugs before streaming hides them: train a
hand-copied debug package with plain gsplat and check the result is upright and at
metric scale. The --backend synthetic option runs the same driver GPU-free for smoke
tests; it does not produce a photorealistic result.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from roomsplat.package import RoomSplatPackage

from .backends import make_backend

log = logging.getLogger("roomsplat.train")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="RoomSplat offline trainer (M2)")
    ap.add_argument("--data", required=True, type=Path, help="path to a .roomsplat package")
    ap.add_argument("--init-ply", default="points3D.ply", help="seed cloud inside the package")
    ap.add_argument("--backend", default="gsplat", choices=["gsplat", "fastgs", "fastergs", "synthetic"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--iters", type=int, default=15000)
    args = ap.parse_args(argv)

    pkg = RoomSplatPackage.open(args.data)
    pkg.validate()
    log.info("opened package: %d frames, camera %dx%d", len(pkg.frames), pkg.camera.w, pkg.camera.h)

    xyz, rgb = pkg.read_point_cloud()
    log.info("seed cloud: %d points", len(xyz))

    t0 = time.monotonic()
    if args.backend == "synthetic":
        backend = make_backend("synthetic", seed_xyz=xyz, seed_rgb=rgb)
    else:
        backend = make_backend(args.backend, package_root=args.data)

    backend.step(args.iters)
    dt = time.monotonic() - t0
    log.info("stage=train backend=%s iters=%d wall_s=%.1f", args.backend, args.iters, dt)

    args.out.mkdir(parents=True, exist_ok=True)
    out_ply = args.out / "point_cloud.ply"
    if hasattr(backend, "write_ply_full"):
        backend.write_ply_full(out_ply)  # full-SH, view-dependent color
    else:
        backend.cloud().write_ply(out_ply)
    log.info("stage=export path=%s splats=%d", out_ply, len(backend.cloud()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
