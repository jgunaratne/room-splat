import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { PointCloudLayer } from "./pointcloud.js";
import { CellManager } from "./cells.js";
import { LiveFrustum } from "./frustum.js";
import { CoverageLayer } from "./coverage.js";
import { RoomMesh } from "./roommesh.js";

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
// Make the canvas keyboard-focusable and give it focus so WASD is received without
// needing to click first (a click also focuses it).
renderer.domElement.tabIndex = 0;
renderer.domElement.style.outline = "none";
renderer.domElement.focus();
renderer.domElement.addEventListener("pointerdown", () => renderer.domElement.focus());

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
const clock = new THREE.Clock();

// WASD fly navigation (works in free mode): W/S forward-back along the view direction,
// A/D strafe, Q/E (or Space/Ctrl) down/up, Shift to move faster. Movement translates the
// camera AND the orbit target together, so mouse-drag still looks around as you fly.
const keys = new Set();
const MOVE_SPEED = 2.5;   // metres/second
const BOOST = 4;          // Shift multiplier
const MOVE_KEYS = new Set(["w", "a", "s", "d", "q", "e", " "]);
addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if (MOVE_KEYS.has(k)) {
    keys.add(k);
    if (mode === "follow") setMode("free"); // take control, like a drag
    if (k === " ") e.preventDefault();      // don't scroll the page
  }
});
addEventListener("keyup", (e) => keys.delete(e.key.toLowerCase()));
addEventListener("blur", () => keys.clear());

function applyFlyMovement(dt) {
  if (keys.size === 0) return;
  const forward = new THREE.Vector3();
  camera.getWorldDirection(forward);
  const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
  const move = new THREE.Vector3();
  if (keys.has("w")) move.add(forward);
  if (keys.has("s")) move.sub(forward);
  if (keys.has("d")) move.add(right);
  if (keys.has("a")) move.sub(right);
  if (keys.has("e") || keys.has(" ")) move.y += 1;
  if (keys.has("q")) move.y -= 1;
  if (move.lengthSq() === 0) return;
  const speed = MOVE_SPEED * (keys.has("shift") ? BOOST : 1) * dt;
  move.normalize().multiplyScalar(speed);
  camera.position.add(move);
  controls.target.add(move);
}
// Shift tracked separately (it isn't a movement key on its own).
addEventListener("keydown", (e) => { if (e.key === "Shift") keys.add("shift"); });
addEventListener("keyup", (e) => { if (e.key === "Shift") keys.delete("shift"); });

// Lights for the LiDAR room mesh (splats/points are unlit and ignore these).
scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 1.1));
const keyLight = new THREE.DirectionalLight(0xffffff, 0.6);
keyLight.position.set(3, 5, 2);
scene.add(keyLight);

const cloud = new PointCloudLayer(scene);
const cells = new CellManager(scene);
const frustum = new LiveFrustum(scene);
const coverage = new CoverageLayer(scene);
const roomMesh = new RoomMesh(scene);
let cellSize = null; // inferred from the first trained cell's bounds

// Two viewing modes (top tabs): the Gaussian-splat scene, and the LiDAR room (a solid
// mesh from ARKit scene reconstruction, falling back to the fused point cloud).
let tab = "splat";
let roomMeshInfo = null; // {session_id, url, version} from the latest room_mesh notice
const tabSplat = document.getElementById("tabSplat");
const tabLidar = document.getElementById("tabLidar");
function setTab(t) {
  tab = t;
  tabSplat.classList.toggle("active", t === "splat");
  tabLidar.classList.toggle("active", t === "lidar");
  const splat = t === "splat";
  cells.setVisible(splat);
  coverage.setEnabled(splat && coverageOn);
  roomMesh.setVisible(!splat);
  // In LiDAR mode the point cloud IS the room, so make it denser-looking; in splat mode
  // it's just the under-layer, kept fine.
  cloud.setPointSize(splat ? 0.015 : 0.03);
  if (!splat && roomMeshInfo) {
    roomMesh.update(roomMeshInfo.url, roomMeshInfo.version).then((info) => {
      if (info) logLine(`LiDAR room mesh: ${info.triangles.toLocaleString()} triangles`);
    });
  }
  logLine(`view: ${splat ? "Gaussian Splat" : "LiDAR room"}`, "dim");
}
tabSplat.onclick = () => setTab("splat");
tabLidar.onclick = () => setTab("lidar");

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

let viewerWs = null;
document.getElementById("reset").onclick = () => {
  // Ask the server to drop the current session + assets; it broadcasts "reset" back,
  // which clears every connected viewer (including this one) via clearScene().
  viewerWs?.send(JSON.stringify({ type: "reset" }));
  logLine("reset requested…", "warn");
};

function clearScene() {
  cells.clear();
  cloud.clear();
  coverage.clear();
  frustum.clear();
  roomMesh.clear();
  roomMeshInfo = null;
  cellSize = null;
  lastCloudUrl = null;
  followTarget = null;
  hidePip();
  setMode("follow");
  countsEl.textContent = "";
  logLine("scene reset — ready for a new capture", "ok");
}
function setMode(m) {
  mode = m;
  controls.enabled = m === "free";
  modeBtn.textContent = `camera: ${m}`;
}
setMode("follow");

// Follow mode locks the camera onto the operator, so orbit is disabled while a session
// streams. Let any interaction take control immediately (switch to free) so the scene
// is inspectable during generation; the button toggles back to follow.
for (const evName of ["pointerdown", "wheel", "touchstart"]) {
  renderer.domElement.addEventListener(
    evName,
    () => { if (mode === "follow") setMode("free"); },
    { passive: true },
  );
}

let followTarget = null;

// Picture-in-picture of the phone's latest keyframe (top-right). The WS only notifies;
// the JPEG is fetched over HTTP, cache-busted by frame_index (server sends no-store).
const pipEl = document.getElementById("pip");
const pipImg = document.getElementById("pipImg");
const pipFrame = document.getElementById("pipFrame");
function showPip(sessionId, frameIndex) {
  pipImg.src = `/live/${sessionId}.jpg?t=${frameIndex}`;
  pipFrame.textContent = `#${frameIndex}`;
  pipEl.style.display = "block";
}
function hidePip() {
  pipEl.style.display = "none";
}

// Bottom-right system console: shows what the pipeline is doing. Fed by server "log"
// messages (session/train/export/finish) plus client-side events (connection, cell
// loads, point-cloud/pose updates). Capped so it can't grow unbounded.
const consoleEl = document.getElementById("console");
const MAX_LOG_LINES = 120;
function logLine(msg, level = "info") {
  const now = new Date();
  const ts = now.toTimeString().slice(0, 8);
  const line = document.createElement("div");
  line.className = "line";
  line.innerHTML = `<span class="t">${ts}</span> <span class="${level}">${escapeHtml(msg)}</span>`;
  consoleEl.appendChild(line);
  while (consoleEl.querySelectorAll(".line").length > MAX_LOG_LINES) {
    consoleEl.querySelector(".line").remove();
  }
  const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
  if (atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

let lastCloudUrl = null;
function onManifest(msg) {
  if (msg.point_cloud_url) {
    if (msg.point_cloud_url !== lastCloudUrl) {
      lastCloudUrl = msg.point_cloud_url;
      cloud.update(msg.point_cloud_url).then((n) => {
        if (n) logLine(`rendered point cloud (${n.toLocaleString()} pts)`);
      });
    }
  }
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
  viewerWs = ws;
  ws.onopen = () => {
    statusEl.textContent = "live";
    logLine("connected to server", "ok");
  };
  ws.onclose = () => {
    statusEl.textContent = "reconnecting…";
    logLine("disconnected — reconnecting…", "warn");
    setTimeout(connect, 1000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "manifest_update") onManifest(msg);
    else if (msg.type === "reset") {
      clearScene();
    } else if (msg.type === "room_mesh") {
      roomMeshInfo = {
        session_id: msg.session_id,
        url: `/room/${msg.session_id}.bin?v=${msg.version}`,
        version: msg.version,
      };
      if (tab === "lidar") {
        roomMesh.update(roomMeshInfo.url, roomMeshInfo.version).then((info) => {
          if (info) logLine(`LiDAR room mesh: ${info.triangles.toLocaleString()} triangles`);
        });
      }
    } else if (msg.type === "log") {
      logLine(msg.msg, msg.level === "warn" ? "warn" : "info");
    } else if (msg.type === "live_pose") {
      const t = frustum.update(msg.pose);
      if (t) followTarget = t;
    } else if (msg.type === "live_frame") {
      showPip(msg.session_id, msg.frame_index);
    } else if (msg.type === "session_complete") {
      statusEl.textContent = "session complete";
      logLine("session complete — camera unlocked", "ok");
      setMode("free");
      hidePip();
    }
  };
}
logLine("viewer ready", "dim");
connect();

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

function animate() {
  requestAnimationFrame(animate);
  const dt = clock.getDelta();
  const flying = keys.size > 0;
  // Apply WASD first, always, so it works regardless of mode/session state.
  applyFlyMovement(dt);
  if (mode === "follow" && followTarget && !flying) {
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
