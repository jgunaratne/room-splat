"""Pin test for the ported ARKit -> Nerfstudio transform (SPEC.md §5).

Pins the output matrix for a known input pose so a future "cleanup" that reintroduces
a handedness flip (the mirrored-room bug) fails loudly here.
"""

import numpy as np

from roomsplat.coords import arkit_pose_to_transform_matrix, opengl_c2w_to_opencv_w2c


def test_pinned_known_pose():
    # simd_float4x4 is column-major: columns[c] is a column. Build a pose with a
    # distinct translation and a 90-degree yaw so transpose errors are visible.
    # Columns (as stored by ARKit): c0, c1, c2, c3(translation).
    col_major = np.array([
        [0.0, 0.0, 1.0, 1.2],   # column 0
        [0.0, 1.0, 0.0, 1.6],   # column 1
        [-1.0, 0.0, 0.0, -3.4],  # column 2
        [0.0, 0.0, 0.0, 1.0],   # column 3
    ])
    out = arkit_pose_to_transform_matrix(col_major)
    # Expected: the transpose (row-major values of the same matrix).
    expected = [
        [0.0, 0.0, -1.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [1.0, 0.0, 0.0, 0.0],
        [1.2, 1.6, -3.4, 1.0],
    ]
    assert out == expected


def test_no_handedness_flip_is_identity_on_identity():
    # An identity ARKit pose must map to an identity transform: any sign flip here
    # is exactly the mirrored-room / inside-out-camera bug from the §5 table.
    out = arkit_pose_to_transform_matrix(np.eye(4))
    assert out == np.eye(4).tolist()


def _project(c2w, K, world_point):
    w2c = opengl_c2w_to_opencv_w2c(c2w)
    x_cam = w2c @ np.array([*world_point, 1.0])
    u = K[0, 0] * x_cam[0] / x_cam[2] + K[0, 2]
    v = K[1, 1] * x_cam[1] / x_cam[2] + K[1, 2]
    return u, v, x_cam[2]


def test_opengl_to_opencv_projection_is_not_mirrored():
    # Identity OpenGL camera at the origin looks down -Z with +X to its right and
    # +Y up. Under the OpenCV convention gsplat uses, a point in front must have
    # positive depth, a point to the world +X must land right of centre, and a point
    # to world +Y (up) must land ABOVE centre (smaller v, since OpenCV y is down).
    W = H = 200
    K = np.array([[300.0, 0, W / 2], [0, 300.0, H / 2], [0, 0, 1]])
    c2w = np.eye(4)

    u_fwd, v_fwd, depth = _project(c2w, K, [0, 0, -2])
    assert depth > 0, "point in front of an OpenGL camera must have +Z depth in OpenCV"
    assert abs(u_fwd - W / 2) < 1e-6 and abs(v_fwd - H / 2) < 1e-6

    u_right, _, _ = _project(c2w, K, [1, 0, -2])
    assert u_right > W / 2, "world +X (camera right) must project right of centre (not mirrored)"

    _, v_up, _ = _project(c2w, K, [0, 1, -2])
    assert v_up < H / 2, "world +Y (up) must project above centre (not flipped)"
