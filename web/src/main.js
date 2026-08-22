import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { PointCloudLayer } from "./pointcloud.js";
import { CellManager } from "./cells.js";
import { LiveFrustum } from "./frustum.js";
import { CoverageLayer } from "./coverage.js";

// The viewer is a pure consumer of the manifest (SPEC.md §4): it holds no derived
// state the server cannot rebuild. A refresh mid-session recovers from the latest
// manifest snapshot the server sends on connect + cached (immutable) asset URLs.

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.05, 500);
camera.position.set(0, 2, 4);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
document.body.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

const cloud = new PointCloudLayer(scene);
const cells = new CellManager(scene);
const frustum = new LiveFrustum(scene);
const coverage = new CoverageLayer(scene);
let cellSize = null; // inferred from the first trained cell's bounds

// Two camera modes (SPEC.md §4): follow tracks the live ARKit pose while a session is
// open; free is a standard orbit. Switch to free on session_complete.
let mode = "follow";
const statusEl = document.getElementById("status");
const countsEl = document.getElementById("counts");
const modeBtn = document.getElementById("mode");
modeBtn.onclick = () => setMode(mode === "follow" ? "free" : "follow");

const coverageBtn = document.getElementById("coverage");
let coverageOn = true;
coverageBtn.onclick = () => {
  coverageOn = !coverageOn;
  coverage.setEnabled(coverageOn);
  coverageBtn.textContent = `coverage: ${coverageOn ? "on" : "off"}`;
};
function setMode(m) {
  mode = m;
  controls.enabled = m === "free";
  modeBtn.textContent = `camera: ${m}`;
}
setMode("follow");

let followTarget = null;

function onManifest(msg) {
  if (msg.point_cloud_url) cloud.update(msg.point_cloud_url);
  if (msg.cells?.length) {
    cells.applyManifest(msg.cells);
    // Infer cell size once from a trained cell's bounds so the coverage layer can box
    // even not-yet-trained under-covered cells without an extra manifest field.
    if (cellSize == null) {
      const b = msg.cells[0].bounds;
      if (b?.length === 6) cellSize = b[3] - b[0];
    }
  }
  if (msg.coverage) coverage.update(msg.coverage, cellSize);
  if (msg.live_pose) {
    const t = frustum.update(msg.live_pose);
    if (t) followTarget = t;
  }
  const undercovered = coverage.count;
  countsEl.textContent =
    `tick ${msg.tick ?? "-"} · ${cells.count} cells` +
    (undercovered ? ` · ${undercovered} under-covered` : "");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/viewer`);
  ws.onopen = () => (statusEl.textContent = "live");
  ws.onclose = () => {
    statusEl.textContent = "reconnecting…";
    setTimeout(connect, 1000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "manifest_update") onManifest(msg);
    else if (msg.type === "live_pose") {
      const t = frustum.update(msg.pose);
      if (t) followTarget = t;
    } else if (msg.type === "session_complete") {
      statusEl.textContent = "session complete";
      setMode("free");
    }
  };
}
connect();

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

function animate() {
  requestAnimationFrame(animate);
  if (mode === "follow" && followTarget) {
    // Ease the browser camera toward the operator so the screen shows what's scanned.
    const desired = followTarget.clone().add(new THREE.Vector3(0, 1.5, 3));
    camera.position.lerp(desired, 0.05);
    camera.lookAt(followTarget);
  } else {
    controls.update();
  }
  renderer.render(scene, camera);
}
animate();
