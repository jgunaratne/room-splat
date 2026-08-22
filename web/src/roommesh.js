import * as THREE from "three";

// LiDAR room mesh (the "3D room" for the LiDAR tab). Rendered from ARKit scene
// reconstruction geometry streamed by the phone. Wire format (room.bin), produced by
// the server from the phone's mesh frame:
//   [uint32 vertexCount][uint32 indexCount]
//   [vertexCount x float32 x,y,z][vertexCount x float32 nx,ny,nz]
//   [vertexCount x uint8 r,g,b]   (present only when the phone samples camera color)
//   [indexCount x uint32]
// little-endian. When colors are present the LiDAR tab shades the polygons; otherwise
// (or in the splat overlay) it draws a cyan wireframe.

// Unpack `count` little-endian float3 vectors at byteOffset with the given byte stride
// into a tightly-packed Float32Array.
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
    this.visible = true;  // shown by default (also overlaid on the splat view)
    this.colored = false; // set true on the LiDAR tab to shade the polygons
    this.hasColor = false; // whether the current mesh carries per-vertex color
    // Cyan wireframe: mirrors the phone's ARKit triangle overlay (scaffold on the splat
    // tab, and the fallback when a mesh has no camera color yet).
    this.wireMat = new THREE.MeshBasicMaterial({
      color: 0x6fd3ff, wireframe: true, transparent: true, opacity: 0.6,
    });
    // Solid, unlit, camera-colored polygons for the LiDAR tab.
    this.colorMat = new THREE.MeshBasicMaterial({
      vertexColors: true, side: THREE.DoubleSide,
    });
  }

  _material() {
    return this.colored && this.hasColor ? this.colorMat : this.wireMat;
  }

  setVisible(on) {
    this.visible = on;
    if (this.mesh) this.mesh.visible = on;
  }

  // Toggle solid camera-colored shading (LiDAR tab) vs the wireframe overlay (splat tab).
  setColored(on) {
    this.colored = on;
    if (this.mesh) this.mesh.material = this._material();
  }

  async update(url, version) {
    if (!url || version <= this.version) return;
    this.version = version;
    this._url = url;
    const buf = await (await fetch(url)).arrayBuffer();
    const view = new DataView(buf);
    const vCount = view.getUint32(0, true);
    const iCount = view.getUint32(4, true);
    // Detect the layout from the total length: with per-vertex color, tight without it,
    // or padded (16-byte Swift SIMD3<Float>) from an older build.
    const withColor = 8 + vCount * 12 * 2 + vCount * 3 + iCount * 4;
    const tight = 8 + vCount * 12 * 2 + iCount * 4;
    const hasColor = buf.byteLength === withColor;
    const stride = hasColor || buf.byteLength === tight ? 12 : 16;

    let o = 8;
    const positions = readVec3(view, o, vCount, stride); o += vCount * stride;
    o += vCount * stride; // skip normals (unused: unlit shading)
    let colors = null;
    if (hasColor) {
      colors = new Uint8Array(buf.slice(o, o + vCount * 3)); o += vCount * 3;
    }
    const indices = new Uint32Array(buf.slice(o, o + iCount * 4));

    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    if (colors) geom.setAttribute("color", new THREE.BufferAttribute(colors, 3, true));
    geom.setIndex(new THREE.BufferAttribute(indices, 1));

    this.hasColor = !!colors;
    const next = new THREE.Mesh(geom, this._material());
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
    this.hasColor = false;
  }
}
