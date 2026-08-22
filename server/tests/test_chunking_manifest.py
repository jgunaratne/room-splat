import numpy as np

from roomsplat.chunking import Chunker, cell_bounds, cell_id
from roomsplat.manifest import Manifest


def test_cell_id_and_bounds_match_spec_format():
    assert cell_id((12, 3, -4)) == "c_12_3_-4"
    assert cell_bounds((12, 3, -4), 1.0) == [12, 3, -4, 13, 4, -3]


def test_assign_creates_occupied_cells():
    ch = Chunker(cell_size=1.0)
    pts = np.array([[0.5, 0.5, 0.5], [1.5, 0.5, 0.5], [0.5, 0.5, 0.5]])
    idx = ch.assign(pts)
    assert idx.shape == (3, 3)
    assert set(ch.cells.keys()) == {(0, 0, 0), (1, 0, 0)}


def test_dirty_selection_respects_budget_and_order():
    ch = Chunker(cell_size=1.0)
    ch.bump_dirty((0, 0, 0), delta=10.0, splats=100)
    ch.bump_dirty((1, 0, 0), delta=1.0, splats=100)
    ch.bump_dirty((2, 0, 0), delta=5.0, splats=100)
    # budget only fits one cell (100 splats * 30 bytes = 3000)
    sel = ch.select_for_export(budget_bytes=3000, bytes_per_splat=30)
    assert [c.index for c in sel] == [(0, 0, 0)]  # dirtiest first


def test_converged_cells_are_not_reexported():
    ch = Chunker(cell_size=1.0)
    ch.bump_dirty((0, 0, 0), delta=10.0, splats=50)
    cell = ch.cells[(0, 0, 0)]
    ch.mark_exported(cell)
    assert cell.version == 1
    assert ch.select_for_export(budget_bytes=10_000) == []


def test_manifest_diff_carries_absolute_versions():
    ch = Chunker(cell_size=1.0)
    ch.bump_dirty((12, 3, -4), delta=4.0, splats=200)
    cell = ch.cells[(12, 3, -4)]
    m = Manifest(session_id="uuid")
    ch.mark_exported(cell)
    entry = m.apply_cell(cell)
    assert entry.version == 1
    assert entry.id == "c_12_3_-4"
    assert entry.url.endswith("c_12_3_-4.v1.spz")
    diff = m.diff([entry])
    assert diff["type"] == "manifest_update"
    assert diff["cells"][0]["version"] == 1
    assert diff["tick"] == 1
