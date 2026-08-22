import * as THREE from "three";

// LiDAR room mesh (the "3D room" for the LiDAR tab). Rendered from ARKit scene
// reconstruction geometry streamed by the phone. Wire format (room.bin), produced by
// the server from the phone's mesh frame:
//   [uint32 vertexCount][uint32 indexCount]
//   [vertexCount x float32 x,y,z][vertexCount x float32 nx,ny,nz][indexCount x uint32]
// little-endian. Falls back to the fused point cloud when no mesh is available yet.

export class RoomMesh {
  constructor(scene) {
    this.scene = scene;
    this.mesh = null;
    this.version = -1;
    this._url = null;
    this.visible = false;
    this.material = new THREE.MeshStandardMaterial({
      color: 0x9aa4b2, roughness: 0.95, metalness: 0.0,
      flatShading: false, side: THREE.DoubleSide,
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
    let o = 8;
    const positions = new Float32Array(buf, o, vCount * 3); o += vCount * 3 * 4;
    const normals = new Float32Array(buf, o, vCount * 3); o += vCount * 3 * 4;
    const indices = new Uint32Array(buf, o, iCount);
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions.slice(), 3));
    geom.setAttribute("normal", new THREE.BufferAttribute(normals.slice(), 3));
    geom.setIndex(new THREE.BufferAttribute(indices.slice(), 1));
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
