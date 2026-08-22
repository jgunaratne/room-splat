import * as THREE from "three";

// LiDAR room mesh (the "3D room" for the LiDAR tab). Rendered from ARKit scene
// reconstruction geometry streamed by the phone. Wire format (room.bin), produced by
// the server from the phone's mesh frame:
//   [uint32 vertexCount][uint32 indexCount]
//   [vertexCount x float32 x,y,z][vertexCount x float32 nx,ny,nz][indexCount x uint32]
// little-endian. Falls back to the fused point cloud when no mesh is available yet.

// Unpack `count` little-endian float3 vectors at byteOffset with the given byte stride
// into a tightly-packed Float32Array (fast path when already tight).
function readVec3(view, off, count, stride) {
  const out = new Float32Array(count * 3);
  for (let k = 0; k < count; k++) {
    const b = off + k * stride;
    out[k * 3] = view.getFloat32(b, true);
    out[k * 3 + 1] = view.getFloat32(b + 4, true);
    out[k * 3 + 2] = view.getFloat32(b + 8, true);
  }
  return out;
}

export class RoomMesh {
  constructor(scene) {
    this.scene = scene;
    this.mesh = null;
    this.version = -1;
    this._url = null;
    this.visible = true; // shown by default (also overlaid on the splat view)
    // Wireframe, to mirror the phone's ARKit scene-reconstruction triangle overlay. As
    // more anchors stream in, each update replaces the mesh with the fuller triangle set,
    // so the wireframe visibly fills out (matching what the operator sees on-device).
    this.material = new THREE.MeshBasicMaterial({
      color: 0x6fd3ff, wireframe: true, transparent: true, opacity: 0.6,
    });
  }

  setVisible(on) {
    this.visible = on;
    if (this.mesh) this.mesh.visible = on;
  }

  async update(url, version) {
    if (!url || version <= this.version) return;
    this.version = version;
    this._url = url;
    const buf = await (await fetch(url)).arrayBuffer();
    const view = new DataView(buf);
    const vCount = view.getUint32(0, true);
    const iCount = view.getUint32(4, true);
    // The vertex stride is 12 bytes (tight xyz) or 16 (Swift SIMD3<Float> padding) —
    // detect it from the total length so either encoder build renders. Normals aren't
    // needed for the unlit wireframe, so we only unpack positions and indices.
    const tight = 8 + vCount * 12 * 2 + iCount * 4;
    const stride = buf.byteLength === tight ? 12 : 16;
    let o = 8;
    const positions = readVec3(view, o, vCount, stride); o += vCount * stride;
    o += vCount * stride; // skip normals
    const indices = new Uint32Array(buf.slice(o, o + iCount * 4));
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geom.setIndex(new THREE.BufferAttribute(indices, 1));
    const next = new THREE.Mesh(geom, this.material);
    next.visible = this.visible;
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.geometry.dispose();
    }
    this.mesh = next;
    this.scene.add(next);
    return { vertices: vCount, triangles: iCount / 3 };
  }

  clear() {
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.geometry.dispose();
      this.mesh = null;
    }
    this.version = -1;
    this._url = null;
  }
}
