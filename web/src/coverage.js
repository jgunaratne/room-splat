import * as THREE from "three";

// Coverage feedback layer (SPEC.md §4). The manifest carries a per-cell
// distinct-viewpoint count; cells seen from fewer than 3 viewpoints are tinted so a
// second person watching the screen can direct the operator to re-scan them. Cells
// that reach 3+ viewpoints drop their tint.
//
// Cell ids encode their integer grid coordinates ("c_<ix>_<iy>_<iz>"), so a box is
// placed even for under-covered cells that have not been trained yet. cellSize is
// inferred from any trained cell's bounds (bounds[3]-bounds[0]) so this stays
// viewer-only with no extra server field.

const WELL_COVERED = 3; // >= this many viewpoints => no tint (SPEC.md §4/M5)
const SEVERITY_COLORS = [0xff3b30, 0xff9500, 0xffcc00]; // 0, 1, 2 viewpoints

function parseCellId(id) {
  // "c_12_3_-4" -> [12, 3, -4]; robust to negative components.
  const parts = id.slice(2).split("_").map(Number);
  return parts.length === 3 && parts.every((n) => Number.isFinite(n)) ? parts : null;
}

export class CoverageLayer {
  constructor(scene) {
    this.scene = scene;
    this.boxes = new Map(); // cell_id -> Mesh
    this.geometry = new THREE.BoxGeometry(1, 1, 1);
    this.enabled = true;
  }

  setEnabled(on) {
    this.enabled = on;
    for (const box of this.boxes.values()) box.visible = on;
  }

  update(coverage, cellSize) {
    if (!coverage || !cellSize) return;
    const seen = new Set();
    for (const [id, count] of Object.entries(coverage)) {
      if (count >= WELL_COVERED) continue;
      const idx = parseCellId(id);
      if (!idx) continue;
      seen.add(id);
      let box = this.boxes.get(id);
      if (!box) {
        const mat = new THREE.MeshBasicMaterial({
          transparent: true, opacity: 0.18, depthWrite: false, side: THREE.DoubleSide,
        });
        box = new THREE.Mesh(this.geometry, mat);
        box.scale.setScalar(cellSize);
        box.position.set(
          (idx[0] + 0.5) * cellSize,
          (idx[1] + 0.5) * cellSize,
          (idx[2] + 0.5) * cellSize,
        );
        box.visible = this.enabled;
        this.scene.add(box);
        this.boxes.set(id, box);
      }
      box.material.color.setHex(SEVERITY_COLORS[Math.max(0, Math.min(2, count))]);
    }
    // Drop tint from cells that are now well covered (or no longer reported).
    for (const [id, box] of this.boxes) {
      if (!seen.has(id)) {
        this.scene.remove(box);
        box.material.dispose();
        this.boxes.delete(id);
      }
    }
  }

  get count() {
    return this.boxes.size;
  }
}
