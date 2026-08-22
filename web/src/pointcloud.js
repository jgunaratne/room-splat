import * as THREE from "three";

// The geometry layer (SPEC.md §4): fused LiDAR cloud, streamed incrementally, drawn
// under the trained cells so there is never a hole where the operator has walked. It
// is never removed during a session, only occluded.
//
// Wire format (cloud.v<n>.bin, produced by server/train/export.py):
//   [uint32 count][count x (float32 x,y,z, uint8 r,g,b)]  little-endian, 15 B/point.

export class PointCloudLayer {
  constructor(scene) {
    this.scene = scene;
    this.points = null;
    this.version = -1;
    this._url = null;
  }

  // Idempotent: only fetch a strictly newer version (versioned, immutable URLs).
  async update(url) {
    if (!url || url === this._url) return;
    this._url = url;
    const res = await fetch(url);
    const buf = await res.arrayBuffer();
    const view = new DataView(buf);
    const count = view.getUint32(0, true);
    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    let o = 4;
    for (let i = 0; i < count; i++) {
      positions[i * 3] = view.getFloat32(o, true);
      positions[i * 3 + 1] = view.getFloat32(o + 4, true);
      positions[i * 3 + 2] = view.getFloat32(o + 8, true);
      colors[i * 3] = view.getUint8(o + 12) / 255;
      colors[i * 3 + 1] = view.getUint8(o + 13) / 255;
      colors[i * 3 + 2] = view.getUint8(o + 14) / 255;
      o += 15;
    }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const mat = new THREE.PointsMaterial({ size: 0.015, vertexColors: true });
    const next = new THREE.Points(geom, mat);
    if (this.points) this.scene.remove(this.points);
    this.points = next;
    this.scene.add(next);
    return count;
  }
}
