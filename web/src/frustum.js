import * as THREE from "three";

// Trajectory + live frustum layer (SPEC.md §4): shows where the operator is right
// now, driven by live ARKit poses from keyframe_meta. Lowest-latency layer.
export class LiveFrustum {
  constructor(scene) {
    this.scene = scene;
    this.frustum = this._makeFrustum();
    scene.add(this.frustum);
    this.trailPositions = [];
    const geom = new THREE.BufferGeometry();
    this.trail = new THREE.Line(geom, new THREE.LineBasicMaterial({ color: 0x4fd1ff }));
    scene.add(this.trail);
  }

  _makeFrustum() {
    // A little camera pyramid pointing down -Z (Nerfstudio/ARKit convention).
    const g = new THREE.ConeGeometry(0.12, 0.24, 4);
    g.rotateX(Math.PI / 2);
    const m = new THREE.MeshBasicMaterial({ color: 0xffd166, wireframe: true });
    return new THREE.Mesh(g, m);
  }

  clear() {
    this.trailPositions = [];
    this.trail.geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(0), 3));
    this.frustum.visible = false;
  }

  // pose is a 4x4 camera-to-world, row-major (as sent on the wire).
  update(pose) {
    if (!pose) return;
    this.frustum.visible = true;
    const m = new THREE.Matrix4();
    // row-major -> THREE column-major set(): pass row by row.
    m.set(
      pose[0][0], pose[0][1], pose[0][2], pose[0][3],
      pose[1][0], pose[1][1], pose[1][2], pose[1][3],
      pose[2][0], pose[2][1], pose[2][2], pose[2][3],
      pose[3][0], pose[3][1], pose[3][2], pose[3][3],
    );
    // The wire matrix is row-major c2w; THREE.set wants row-major too, but our stored
    // form transposes translation into the last row, so decompose accordingly.
    const t = new THREE.Vector3(pose[3][0], pose[3][1], pose[3][2]);
    this.frustum.position.copy(t);
    const rot = new THREE.Matrix4().set(
      pose[0][0], pose[1][0], pose[2][0], 0,
      pose[0][1], pose[1][1], pose[2][1], 0,
      pose[0][2], pose[1][2], pose[2][2], 0,
      0, 0, 0, 1,
    );
    this.frustum.quaternion.setFromRotationMatrix(rot);

    // Append to the trail only when the camera actually moved, so the low-latency
    // live_pose stream and the periodic manifest snapshot (which carry the same latest
    // pose) don't double-add coincident points.
    const p = this.trailPositions;
    const n = p.length;
    if (n < 3 || Math.hypot(t.x - p[n - 3], t.y - p[n - 2], t.z - p[n - 1]) > 0.01) {
      p.push(t.x, t.y, t.z);
      this.trail.geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(p), 3));
    }
    return t;
  }
}
