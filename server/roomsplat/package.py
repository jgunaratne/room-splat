"""The `.roomsplat` data contract (SPEC.md §6).

One schema, one parser, one set of tests. This module is the single source of truth
for both the debug-mode writer (M1, on device) and the streaming disk mirror (M3+).
The on-disk layout is byte-identical between the two by construction: the streaming
path just appends through the same writer.

    <uuid>.roomsplat/
    ├── capture.json          # our metadata
    ├── transforms.json       # Nerfstudio-style camera model + frames
    ├── thumbnail.jpg
    ├── images/frame_00000.jpg ...
    └── points3D.ply          # fused LiDAR cloud, XYZRGB
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({2})

# transforms.json top-level keys the reference writer emits. gsplat's nerfstudio
# parser reads these; keep the names exactly.
_TRANSFORM_TOP_KEYS = ("camera_model", "fl_x", "fl_y", "cx", "cy", "w", "h")


class PackageError(ValueError):
    """Raised when a package violates the data contract."""


@dataclass
class CameraModel:
    camera_model: str
    fl_x: float
    fl_y: float
    cx: float
    cy: float
    w: int
    h: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_model": self.camera_model,
            "fl_x": self.fl_x,
            "fl_y": self.fl_y,
            "cx": self.cx,
            "cy": self.cy,
            "w": self.w,
            "h": self.h,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CameraModel":
        missing = [k for k in _TRANSFORM_TOP_KEYS if k not in d]
        if missing:
            raise PackageError(f"transforms.json missing top-level keys: {missing}")
        return cls(
            camera_model=str(d["camera_model"]),
            fl_x=float(d["fl_x"]),
            fl_y=float(d["fl_y"]),
            cx=float(d["cx"]),
            cy=float(d["cy"]),
            w=int(d["w"]),
            h=int(d["h"]),
        )


@dataclass
class Frame:
    """One keyframe: image path plus its 4x4 camera-to-world (row-major)."""

    file_path: str
    transform_matrix: list[list[float]]

    def to_dict(self) -> dict[str, Any]:
        return {"file_path": self.file_path, "transform_matrix": self.transform_matrix}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Frame":
        if "file_path" not in d or "transform_matrix" not in d:
            raise PackageError("frame missing file_path or transform_matrix")
        m = d["transform_matrix"]
        if len(m) != 4 or any(len(row) != 4 for row in m):
            raise PackageError(f"transform_matrix must be 4x4, got {m}")
        return cls(file_path=str(d["file_path"]), transform_matrix=[[float(x) for x in row] for row in m])


def validate_capture(meta: dict[str, Any]) -> None:
    """Validate capture.json. Rejects unknown schema versions loudly (§6)."""
    version = meta.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PackageError(
            f"unsupported schema_version {version!r}; this server supports {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    for key in ("session_id", "device_model", "captured_at", "source"):
        if key not in meta:
            raise PackageError(f"capture.json missing required key: {key}")
    if meta["source"] not in ("stream", "debug"):
        raise PackageError(f"capture.json source must be 'stream' or 'debug', got {meta['source']!r}")


class RoomSplatPackage:
    """Reader/writer/validator for a `.roomsplat` directory.

    The streaming mirror opens one of these and calls `append_keyframe` as binary
    frames arrive; transforms.json is rewritten atomically after every append so it
    is always valid JSON (§6). The debug-mode writer on device produces the same
    layout, so the same reader validates both.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.images_dir = self.root / "images"

    # ---- construction -------------------------------------------------------

    @classmethod
    def create(cls, root: Path, capture: dict[str, Any], camera: CameraModel) -> "RoomSplatPackage":
        validate_capture(capture)
        pkg = cls(root)
        pkg.images_dir.mkdir(parents=True, exist_ok=True)
        pkg._camera = camera
        pkg._frames = []
        pkg._capture = dict(capture)
        pkg._write_capture()
        pkg._write_transforms()
        return pkg

    @classmethod
    def open(cls, root: Path) -> "RoomSplatPackage":
        pkg = cls(root)
        pkg._capture = json.loads((pkg.root / "capture.json").read_text())
        validate_capture(pkg._capture)
        transforms = json.loads((pkg.root / "transforms.json").read_text())
        pkg._camera = CameraModel.from_dict(transforms)
        pkg._frames = [Frame.from_dict(f) for f in transforms.get("frames", [])]
        return pkg

    # ---- streaming append ---------------------------------------------------

    def append_keyframe(self, index: int, jpeg: bytes, transform_matrix: list[list[float]]) -> str:
        """Write one keyframe image and register its pose. Returns the file_path.

        transforms.json is rewritten atomically so a reader (or a crashed session
        reopened later) always sees valid JSON.
        """
        name = f"frame_{index:05d}.jpg"
        (self.images_dir / name).write_bytes(jpeg)
        if not (self.root / "thumbnail.jpg").exists():
            (self.root / "thumbnail.jpg").write_bytes(jpeg)
        file_path = f"images/{name}"
        self._frames.append(Frame(file_path=file_path, transform_matrix=transform_matrix))
        self._write_transforms()
        return file_path

    def write_point_cloud(self, xyz: np.ndarray, rgb: np.ndarray) -> None:
        """Write/overwrite points3D.ply (binary_little_endian XYZRGB)."""
        write_ply(self.root / "points3D.ply", xyz, rgb)

    def update_capture(self, **fields: Any) -> None:
        self._capture.update(fields)
        self._write_capture()

    # ---- accessors ----------------------------------------------------------

    @property
    def camera(self) -> CameraModel:
        return self._camera

    @property
    def frames(self) -> list[Frame]:
        return list(self._frames)

    @property
    def capture(self) -> dict[str, Any]:
        return dict(self._capture)

    def read_point_cloud(self) -> tuple[np.ndarray, np.ndarray]:
        return read_ply(self.root / "points3D.ply")

    # ---- validation ---------------------------------------------------------

    def validate(self) -> None:
        """Full structural validation against §6. Raises PackageError on any issue."""
        validate_capture(self._capture)
        if not self.images_dir.is_dir():
            raise PackageError("images/ directory missing")
        for frame in self._frames:
            img = self.root / frame.file_path
            if not img.exists():
                raise PackageError(f"frame references missing image: {frame.file_path}")
            if len(frame.transform_matrix) != 4 or any(len(r) != 4 for r in frame.transform_matrix):
                raise PackageError(f"bad transform_matrix for {frame.file_path}")
        declared = self._capture.get("frame_count")
        if declared is not None and declared != len(self._frames):
            raise PackageError(f"frame_count={declared} but transforms.json has {len(self._frames)} frames")

    # ---- internals ----------------------------------------------------------

    def _write_capture(self) -> None:
        _atomic_write_text(self.root / "capture.json", json.dumps(self._capture, indent=2, sort_keys=True))

    def _write_transforms(self) -> None:
        doc = self._camera.to_dict()
        doc["ply_file_path"] = "points3D.ply"
        doc["frames"] = [f.to_dict() for f in self._frames]
        _atomic_write_text(self.root / "transforms.json", json.dumps(doc, indent=2, sort_keys=True))


# ---- PLY I/O ----------------------------------------------------------------
# binary_little_endian XYZRGB. Matches what MeshLab and gsplat's PLY reader expect.

_PLY_HEADER = (
    "ply\n"
    "format binary_little_endian 1.0\n"
    "element vertex {n}\n"
    "property float x\nproperty float y\nproperty float z\n"
    "property uchar red\nproperty uchar green\nproperty uchar blue\n"
    "end_header\n"
)


def write_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    xyz = np.asarray(xyz, dtype=np.float32).reshape(-1, 3)
    rgb = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3)
    if len(xyz) != len(rgb):
        raise PackageError(f"xyz/rgb length mismatch: {len(xyz)} vs {len(rgb)}")
    header = _PLY_HEADER.format(n=len(xyz)).encode("ascii")
    dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    packed = np.empty(len(xyz), dtype=dtype)
    packed["x"], packed["y"], packed["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    packed["r"], packed["g"], packed["b"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    with open(path, "wb") as f:
        f.write(header)
        f.write(packed.tobytes())


def read_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with open(path, "rb") as f:
        # Parse the ASCII header line by line.
        header_lines = []
        while True:
            line = f.readline()
            if not line:
                raise PackageError("truncated PLY: no end_header")
            header_lines.append(line)
            if line.strip() == b"end_header":
                break
        text = b"".join(header_lines).decode("ascii", "replace")
        if "binary_little_endian" not in text:
            raise PackageError("only binary_little_endian PLY is supported by read_ply")
        n = 0
        for hl in header_lines:
            if hl.startswith(b"element vertex"):
                n = int(hl.split()[2])
        dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
        buf = f.read(n * dtype.itemsize)
        data = np.frombuffer(buf, dtype=dtype, count=n)
    xyz = np.stack([data["x"], data["y"], data["z"]], axis=1).astype(np.float32)
    rgb = np.stack([data["r"], data["g"], data["b"]], axis=1).astype(np.uint8)
    return xyz, rgb


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
