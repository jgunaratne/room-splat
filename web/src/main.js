import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { PointerLockControls } from "three/addons/controls/PointerLockControls.js";
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

// Orbit is the secondary "inspect" mode; fly (pointer-lock FPS) is the default free-look.
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.enabled = false;
const clock = new THREE.Clock();

// Fly navigation: game-style pointer-lock mouselook + WASD (SPEC.md §4 free viewing).
// Click the canvas to capture the mouse and look; Esc releases. WASD walks on the
// horizontal plane relative to where you're facing (looking down never dives), Space/E
// up and Ctrl/Q down, Shift to sprint, mouse wheel to change speed.
const fly = new PointerLockControls(camera, renderer.domElement);
fly.pointerSpeed = 0.8;                 // mouse sensitivity

const keys = new Set();
const moveSpeed = 6;                     // metres/second (WASD)
const BOOST = 4;                         // Shift multiplier
const ACCEL = 12;                        // velocity ramp (1/s) for precise taps, fast holds
const ZOOM_STEP = 0.5;                   // metres dollied per wheel notch
const velocity = new THREE.Vector3();
const MOVE_KEYS = new Set(["w", "a", "s", "d", "q", "e", " ", "control"]);

addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if (k === "shift") { keys.add("shift"); return; }
  if (!MOVE_KEYS.has(k)) return;
  keys.add(k);
  if (mode === "follow") setMode("fly");   // take control, like a drag
  if (k === " " || k === "control") e.preventDefault(); // don't scroll / trigger shortcuts
});
addEventListener("keyup", (e) => keys.delete(e.key.toLowerCase()));
addEventListener("blur", () => { keys.clear(); velocity.set(0, 0, 0); });

// Wheel zooms by dollying the camera along the view direction (orbit keeps its own zoom).
renderer.domElement.addEventListener("wheel", (e) => {
  if (mode !== "fly") return;
  e.preventDefault();
  const dir = camera.getWorldDirection(new THREE.Vector3());
  const step = ZOOM_STEP * (e.deltaY < 0 ? 1 : -1) * (keys.has("shift") ? BOOST : 1);
  camera.position.addScaledVector(dir, step);
}, { passive: false });

function applyFlyMovement(dt) {
  if (mode === "follow") { velocity.set(0, 0, 0); return; }
  // Horizontal forward/right from yaw only, so pitch never tilts movement.
  const forward = new THREE.Vector3();
  camera.getWorldDirection(forward);
  forward.y = 0;
  if (forward.lengthSq() < 1e-6) forward.set(0, 0, -1); // looking straight up/down
  forward.normalize();
  const right = new THREE.Vector3(-forward.z, 0, forward.x); // cross(forward, up): screen-right
  const wish = new THREE.Vector3();
  if (keys.has("w")) wish.add(forward);
  if (keys.has("s")) wish.sub(forward);
  if (keys.has("d")) wish.add(right);
  if (keys.has("a")) wish.sub(right);
  if (keys.has("e") || keys.has(" ")) wish.y += 1;
  if (keys.has("q") || keys.has("control")) wish.y -= 1;
  const target = wish.lengthSq() > 0
    ? wish.normalize().multiplyScalar(moveSpeed * (keys.has("shift") ? BOOST : 1))
    : wish.set(0, 0, 0);
  // Ease current velocity toward the target for a crisp-but-not-jerky ramp.
  velocity.lerp(target, 1 - Math.exp(-ACCEL * dt));
  if (velocity.lengthSq() < 1e-8) return;
  const delta = velocity.clone().multiplyScalar(dt);
  camera.position.add(delta);
  if (mode === "orbit") controls.target.add(delta); // keep orbit pivot ahead of the camera
}

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
  // The LiDAR mesh stays visible in both tabs: on the splat tab it is a live wireframe
  // scaffold the splats fill in; on the LiDAR tab it is the room, shaded with the
  // camera-sampled polygon colors.
  roomMesh.setVisible(true);
  roomMesh.setColored(!splat);
  // In LiDAR mode the point cloud IS the room, so make it denser-looking; in splat mode
  // it's just the under-layer, kept fine.
  cloud.setPointSize(splat ? 0.015 : 0.03);
  if (roomMeshInfo) {
    roomMesh.update(roomMeshInfo.url, roomMeshInfo.version).then((info) => {
      if (info) logLine(`LiDAR room mesh: ${info.triangles.toLocaleString()} triangles`);
    });
  }
  logLine(`view: ${splat ? "Gaussian Splat" : "LiDAR room"}`, "dim");
}
tabSplat.onclick = () => setTab("splat");
tabLidar.onclick = () => setTab("lidar");

// Camera modes (SPEC.md §4): follow tracks the live ARKit pose while a session is open;
// fly is game-style pointer-lock free-look (the default free mode); orbit is the
// secondary inspect mode. The button cycles follow → fly → orbit.
let mode = "follow";
const statusEl = document.getElementById("status");
const countsEl = document.getElementById("counts");
const modeBtn = document.getElementById("mode");
const NEXT_MODE = { follow: "fly", fly: "orbit", orbit: "follow" };
modeBtn.onclick = () => setMode(NEXT_MODE[mode]);

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

// Saved projects: snapshot the current scene (splat cells + LiDAR mesh) server-side,
// list them, and load one back for inspection without disturbing a live capture.
let currentSessionId = null;
const projectsSel = document.getElementById("projects");

async function refreshProjects() {
  try {
    const list = await (await fetch("/projects")).json();
    projectsSel.innerHTML = '<option value="">— saved projects —</option>';
    for (const p of list) {
      const o = document.createElement("option");
      o.value = p.id;
      const when = new Date((p.updated || 0) * 1000).toLocaleString();
      o.textContent = `${p.name} · ${p.cells} cells${p.has_mesh ? " · mesh" : ""} · ${when}`;
      projectsSel.appendChild(o);
    }
  } catch { /* offline; leave the list as-is */ }
}

document.getElementById("save").onclick = () => {
  if (!currentSessionId) { logLine("nothing to save yet", "warn"); return; }
  const name = prompt("Save project as:", `room ${new Date().toLocaleString()}`);
  if (name == null) return;
  viewerWs?.send(JSON.stringify({ type: "save_project", session_id: currentSessionId, name }));
};

document.getElementById("load").onclick = () => {
  const id = projectsSel.value;
  if (!id) { logLine("pick a saved project first", "warn"); return; }
  clearForLoad();
  viewerWs?.send(JSON.stringify({ type: "load_project", id }));
  logLine("loading saved project…", "dim");
};

// Clear the viewer for a load without asking the server to drop the live session.
function clearForLoad() {
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
  setMode("fly"); // saved projects are static — free-look, not follow
  countsEl.textContent = "";
}

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
function refreshModeLabel() {
  modeBtn.textContent = `camera: ${mode}`;
}
function setMode(m) {
  mode = m;
  controls.enabled = m === "orbit";
  if (m === "orbit") {
    // Put the orbit pivot a few metres ahead of the camera so it rotates about what
    // you were looking at, not a stale target left over from the last orbit session.
    const ahead = camera.getWorldDirection(new THREE.Vector3()).multiplyScalar(3);
    controls.target.copy(camera.position).add(ahead);
    controls.update();
  }
  if (m !== "fly" && fly.isLocked) fly.unlock();
  refreshModeLabel();
}
setMode("follow");

// Follow mode locks the camera onto the operator. Any interaction takes control (→ fly)
// so the scene is inspectable during generation; the button cycles modes.
renderer.domElement.addEventListener("pointerdown", () => {
  renderer.domElement.focus();
  if (mode === "follow") setMode("fly");
  if (mode === "fly" && !fly.isLocked) fly.lock(); // capture the mouse for free-look
});
for (const evName of ["wheel", "touchstart"]) {
  renderer.domElement.addEventListener(
    evName,
    () => { if (mode === "follow") setMode("fly"); },
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
  if (msg.session_id) currentSessionId = msg.session_id;
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
    refreshProjects();
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
      // Live meshes are served from /room/<id>.bin; a saved project carries an explicit
      // /projects-assets URL instead.
      const base = msg.url || `/room/${msg.session_id}.bin`;
      roomMeshInfo = {
        session_id: msg.session_id,
        url: `${base}?v=${msg.version}`,
        version: msg.version,
      };
      // Update live in either tab so the wireframe fills out as the phone scans.
      roomMesh.update(roomMeshInfo.url, roomMeshInfo.version).then((info) => {
        if (info) logLine(`LiDAR room mesh: ${info.triangles.toLocaleString()} triangles`);
      });
    } else if (msg.type === "projects") {
      refreshProjects();
    } else if (msg.type === "log") {
      logLine(msg.msg, msg.level === "warn" ? "warn" : "info");
    } else if (msg.type === "live_pose") {
      if (msg.session_id) currentSessionId = msg.session_id;
      const t = frustum.update(msg.pose);
      if (t) followTarget = t;
    } else if (msg.type === "live_frame") {
      showPip(msg.session_id, msg.frame_index);
    } else if (msg.type === "session_complete") {
      statusEl.textContent = "session complete";
      logLine("session complete — camera unlocked (click to look, WASD to move)", "ok");
      setMode("fly");
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
  const dt = Math.min(clock.getDelta(), 0.1); // clamp so a stalled tab doesn't teleport
  applyFlyMovement(dt);
  if (mode === "follow" && followTarget) {
    // Ease the browser camera toward the operator so the screen shows what's scanned.
    const desired = followTarget.clone().add(new THREE.Vector3(0, 1.5, 3));
    camera.position.lerp(desired, 0.05);
    camera.lookAt(followTarget);
  } else if (mode === "orbit") {
    controls.update(); // damping; fly updates the camera directly via pointer lock + WASD
  }
  renderer.render(scene, camera);
}
animate();
