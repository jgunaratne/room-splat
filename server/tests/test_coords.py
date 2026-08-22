"""Pin test for the ported ARKit -> Nerfstudio transform (SPEC.md §5).

Pins the output matrix for a known input pose so a future "cleanup" that reintroduces
a handedness flip (the mirrored-room bug) fails loudly here.
"""

import numpy as np

from roomsplat.coords import arkit_pose_to_transform_matrix


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
