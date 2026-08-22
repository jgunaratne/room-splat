"""Coordinate transform, ported verbatim from the reference iOS writer (SPEC.md §5).

Source: vendor/ios-gaussian-splatting-demo/3DGS Demo/Capture/GaussianCaptureView.swift
        `extension simd_float4x4 { var rowMajorValues }` and the seed-point back-projection.

Do not re-derive this from the handedness table in §5. ARKit already uses the
Nerfstudio/OpenGL convention (right-handed, +Y up, camera looks -Z), which is exactly
what gsplat's nerfstudio parser expects, so the "transform" is a pure column-major ->
row-major relayout of the ARKit camera-to-world matrix with NO handedness flip. That
absence is the point: adding a flip here is what produces a mirrored room (§5 table).

The Swift version is the source of truth; this is the server-side mirror used to
validate streamed poses and pinned by a unit test (test_coords.py).
"""

from __future__ import annotations

import numpy as np


def arkit_pose_to_transform_matrix(pose_column_major: np.ndarray) -> list[list[float]]:
    """ARKit camera transform (column-major 4x4) -> row-major camera-to-world list.

    `pose_column_major[c][r]` is column c, row r (simd_float4x4 layout). The reference
    writer emits `self[column][row]` for row, column in 0..4, i.e. the row-major values
    of the same matrix. In numpy terms that is the transpose of the column-major store.
    """
    m = np.asarray(pose_column_major, dtype=np.float64).reshape(4, 4)
    row_major = m.T
    return [[float(x) for x in row] for row in row_major]


def backproject_depth_sample(
    image_x: float,
    image_y: float,
    z: float,
    intrinsics: np.ndarray,
    camera_to_world: np.ndarray,
) -> np.ndarray:
    """Port of copySeedPoints back-projection: pixel + depth -> world point.

    Note the y flip `(cy - imageY)` and the -z camera forward, matching ARKit's -Z
    look direction. intrinsics and camera_to_world are column-major (simd layout).
    """
    K = np.asarray(intrinsics, dtype=np.float64).reshape(3, 3)
    c2w = np.asarray(camera_to_world, dtype=np.float64).reshape(4, 4)
    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[2, 0], K[2, 1]  # column-major: cx is columns.2.x -> K[2,0]
    camera_x = (image_x - cx) * z / fx
    camera_y = (cy - image_y) * z / fy
    cam = np.array([camera_x, camera_y, -z, 1.0])
    world = c2w.T @ cam  # c2w stored column-major; .T gives standard row-major matrix
    return world[:3]
