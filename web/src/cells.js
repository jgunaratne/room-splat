import { SplatMesh } from "@sparkjsdev/spark";

// The splat layer (SPEC.md §4): each cell is an independent Spark SplatMesh, loaded
// from a versioned immutable URL over plain HTTP. Viewer-side rules enforced here:
//   - apply only versions newer than what we hold (idempotent, order-independent);
//   - fetch newest-first, never queue: abandon an older fetch if a newer arrives;
//   - keep the current mesh mounted until the replacement parses, then cross-fade
//     ~200 ms and dispose the old one (no black hole on update);
//   - cap concurrent parses at 2 (SPZ/PLY parsing is main-thread work).

const CROSSFADE_MS = 200;
const MAX_CONCURRENT_PARSES = 2;

class CellSlot {
  constructor(id) {
    this.id = id;
    this.version = -1;      // currently mounted
    this.wantVersion = -1;  // latest requested
    this.wantUrl = null;
    this.mesh = null;
    this.loading = false;
  }
}

export class CellManager {
  constructor(scene) {
    this.scene = scene;
    this.slots = new Map();
    this._active = 0;
  }

  applyManifest(cells) {
    for (const c of cells) {
      let slot = this.slots.get(c.id);
      if (!slot) { slot = new CellSlot(c.id); this.slots.set(c.id, slot); }
      if (c.version <= Math.max(slot.version, slot.wantVersion)) continue; // stale
      slot.wantVersion = c.version;
      slot.wantUrl = c.url;
    }
    this._pump();
  }

  _pump() {
    if (this._active >= MAX_CONCURRENT_PARSES) return;
    // newest-first: prioritize the slot whose pending version is highest.
    const pending = [...this.slots.values()]
      .filter((s) => !s.loading && s.wantVersion > s.version)
      .sort((a, b) => b.wantVersion - a.wantVersion);
    for (const slot of pending) {
      if (this._active >= MAX_CONCURRENT_PARSES) break;
      this._load(slot);
    }
  }

  async _load(slot) {
    slot.loading = true;
    this._active++;
    const version = slot.wantVersion;
    const url = slot.wantUrl;
    try {
      const mesh = new SplatMesh({ url });
      await mesh.initialized;
      // A newer version was requested while we parsed: abandon this one.
      if (slot.wantVersion > version) {
        mesh.dispose?.();
      } else {
        this._mount(slot, mesh, version);
      }
    } catch (err) {
      console.warn("cell load failed", slot.id, err);
    } finally {
      slot.loading = false;
      this._active--;
      this._pump();
    }
  }

  _mount(slot, mesh, version) {
    mesh.opacity = 0;
    this.scene.add(mesh);
    const old = slot.mesh;
    slot.mesh = mesh;
    slot.version = version;
    const start = performance.now();
    const tick = () => {
      const t = Math.min(1, (performance.now() - start) / CROSSFADE_MS);
      mesh.opacity = t;
      if (old) old.opacity = 1 - t;
      if (t < 1) requestAnimationFrame(tick);
      else if (old) { this.scene.remove(old); old.dispose?.(); }
    };
    requestAnimationFrame(tick);
  }

  get count() { return this.slots.size; }

  clear() {
    for (const slot of this.slots.values()) {
      if (slot.mesh) { this.scene.remove(slot.mesh); slot.mesh.dispose?.(); }
    }
    this.slots.clear();
    this._active = 0;
  }
}
