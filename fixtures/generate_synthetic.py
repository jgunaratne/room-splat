#!/usr/bin/env python3
"""Generate synthetic .roomsplat fixtures for CI replay testing.

Creates three small deterministic packages (different room shapes, frame counts,
and point densities) so the replay pipeline exercises chunking, training, and
export under varied conditions — all without a real device capture.

Once real captures land in fixtures/, this script can be retired or kept as a
fallback for environments without LFS.

Usage:
    python fixtures/generate_synthetic.py [--out fixtures/]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

# The server package is one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from roomsplat.package import CameraModel, RoomSplatPackage  # noqa: E402


def _tiny_jpeg() -> bytes:
    """Smallest valid JPEG payload; the pipeline treats images as opaque bytes."""
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


# Three fixtures with varying room geometries, walk paths, and densities.
FIXTURE_SPECS = [
    {
        "name": "small_hallway",
        "n_frames": 8,
        "n_points": 2000,
        "room_min": [-1, 0, -5],
        "room_max": [1, 3, 0],
        "walk": lambda t: [0.0, 1.6, -4.5 + 4.0 * t],  # walk along +z
    },
    {
        "name": "medium_office",
        "n_frames": 15,
        "n_points": 4000,
        "room_min": [-3, 0, -3],
        "room_max": [3, 3, 3],
        "walk": lambda t: [-2.5 + 5.0 * t, 1.6, -1.0],  # walk along +x
    },
    {
        "name": "large_lshape",
        "n_frames": 24,
        "n_points": 6000,
        "room_min": [-4, 0, -4],
        "room_max": [4, 3, 4],
        "walk": lambda t: [                               # L-shaped walk
            -3.0 + 6.0 * min(t * 2, 1.0),
            1.6,
            -3.0 + 6.0 * max(0.0, (t - 0.5) * 2),
        ],
    },
]


def generate_fixture(out_dir: Path, spec: dict, seed: int) -> Path:
    rng = np.random.default_rng(seed)
    camera = CameraModel(
        camera_model="OPENCV", fl_x=1400.0, fl_y=1400.0,
        cx=800.0, cy=600.0, w=1600, h=1200,
    )
    capture = {
        "schema_version": 2,
        "session_id": spec["name"],
        "device_model": "synthetic-ci",
        "captured_at": "2026-01-01T00:00:00Z",
        "source": "debug",
    }
    root = out_dir / f"{spec['name']}.roomsplat"
    if root.exists():
        import shutil
        shutil.rmtree(root)
    pkg = RoomSplatPackage.create(root, capture, camera)

    xyz = rng.uniform(spec["room_min"], spec["room_max"],
                      size=(spec["n_points"], 3)).astype(np.float32)
    rgb = rng.integers(0, 255, size=(spec["n_points"], 3), dtype=np.uint8)
    pkg.write_point_cloud(xyz, rgb)

    jpeg = _tiny_jpeg()
    for i in range(spec["n_frames"]):
        t = i / max(1, spec["n_frames"] - 1)
        pos = spec["walk"](t)
        pose = np.eye(4, dtype=np.float32)
        pose[:3, 3] = pos
        pkg.append_keyframe(
            i, jpeg, [[float(v) for v in row] for row in pose]
        )

    pkg.update_capture(
        frame_count=spec["n_frames"], seed_point_count=spec["n_points"]
    )
    pkg.validate()
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic CI fixtures")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent,
                    help="Directory to write fixtures into (default: fixtures/)")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for i, spec in enumerate(FIXTURE_SPECS):
        root = generate_fixture(args.out, spec, seed=42 + i)
        print(f"  ✓ {root.name}  ({spec['n_frames']} frames, {spec['n_points']} pts)")
    print(f"\nGenerated {len(FIXTURE_SPECS)} fixtures in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
