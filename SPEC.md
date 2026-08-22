# SPEC.md — RoomSplat

Capture a room with an iPhone Pro (ARKit + LiDAR), stream it live to a desktop running
a webserver and an RTX 5090, train a 3D Gaussian Splat progressively, and render it in
the browser with three.js + Spark. Both devices do real work.

---

## 0. Non-negotiables

Read these before writing any code.

1. **Do not implement Structure-from-Motion.** No COLMAP feature extraction, no
   matching, no `pycolmap` reconstruction in the happy path. ARKit supplies camera
   poses and intrinsics directly. Removing SfM is the entire reason this pipeline can
   keep up with a live walkthrough. The only permitted COLMAP usage is the optional
   pose-refinement pass in M7, which takes ARKit poses as fixed initialization.
2. **On-device training is preview-only.** The phone may train a coarse splat for
   immediate in-hand feedback. That artifact is never the deliverable, never uploaded,
   and never rendered in the browser. The desktop result is the only output that ships.
   See §2 for the split policy.
3. **Do not build the viewer on `antimatter15/splat` or `mkkellogg/GaussianSplats3D`.**
   Use `@sparkjsdev/spark`. mkkellogg's repo is explicitly unmaintained and its README
   redirects to Spark.
4. **Do not invent the coordinate transform.** Port it verbatim from the reference
   dataset writer. See §5.
5. **Do not skip a milestone gate.** Each gate in §3 has an acceptance test. A gate is
   passed when the test passes, not when the code looks right. Most failures in this
   pipeline surface two stages downstream of their cause, and live streaming makes that
   worse by removing the inspectable files between stages — which is why §4 mandates
   that streaming also writes to disk.
6. **Licensing.** `Voxelio-app/ios-gaussian-splatting-demo` is PolyForm Noncommercial
   1.0.0. This project is personal/non-commercial. Do not vendor its code into anything
   that could ship commercially, and preserve its `THIRD_PARTY_NOTICES.md` in any
   directory containing derived code.

---

## 1. Repository layout

Single repo, three packages, no shared build system between them.

```
roomsplat/
├── SPEC.md                  # this file
├── AGENTS.md                # agent roles + handoff protocol
├── ios/                     # Xcode project
│   └── RoomSplat/
│       ├── Capture/         # ARSession, keyframe selection, LiDAR fusion
│       ├── Stream/          # live upload client, backpressure, reconnect
│       ├── Preview/         # on-device coarse training + MetalSplatter view
│       ├── Governor/        # thermal + battery work-shedding
│       └── Views/
├── server/                  # Python, runs on the 5090 box
│   ├── app/                 # FastAPI: static host, ingest WS, viewer WS
│   ├── ingest/              # frame receiver, session store, disk mirror
│   ├── train/               # gsplat / FastGS driver, progressive checkpoints
│   ├── simplify/            # NanoGS invocation
│   └── pyproject.toml
└── web/                     # Vite + three.js + Spark viewer
    ├── src/
    └── package.json
```

Reference repos are cloned to `./vendor/` and are **read-only**. Never edit vendored
code; copy what you need into our tree with attribution.

```
vendor/
├── ios-gaussian-splatting-demo/   # capture, coordinate transform, msplat, MetalSplatter
├── NanoGS/                        # simplification — use saliteta/NanoGS for the python impl
└── spark/                         # viewer reference
```

---

## 2. Work split policy

Both devices process. The split is not negotiable per-feature — it follows one rule:

> Work that **reduces bytes on the wire** or needs **sub-second feedback in the
> operator's hand** runs on the phone. Work that needs **the GPU** or **the complete
> scene** runs on the desktop.

| Stage | Where | Why |
|---|---|---|
| Keyframe selection (pose delta, blur score, tracking state) | iPhone | Rejects ~80% of frames before they cost bandwidth |
| JPEG encode + downscale to 1600 px | iPhone | Same |
| LiDAR depth fusion, voxel downsample, plane extraction | iPhone | Depth maps are huge; the fused cloud is small |
| Coverage analysis ("you haven't seen the far corner") | iPhone | Needs to be in the operator's hand while they can still act on it |
| Coarse preview splat, 1–2k iterations | iPhone | Instant feedback; desktop is busy with the real one |
| Full training, 15k+ iterations | Desktop | Needs the 5090 |
| Progressive checkpoint → per-cell SPZ export | Desktop | Needs the trained model |
| Cell dirty-scoring and manifest diffing | Desktop | Needs the full parameter history |
| NanoGS simplification | Desktop | Needs the complete scene |
| Pose refinement / bundle adjustment | Desktop | Needs the complete trajectory |
| Serving the viewer | Desktop | It is the webserver |

**Capability negotiation.** The phone reports its capabilities at session open
(`device_model`, thermal headroom, whether preview training is enabled). The server
assigns stages from the table above and may withhold preview training if the desktop
is idle enough to render previews faster over the wire. Do not hardcode the assignment
on the client.

**Thermal governor (required, not optional).** Sustained ARKit + LiDAR + camera +
Metal training + continuous upload will thermally throttle an iPhone within a few
minutes, and throttling degrades ARKit tracking, which corrupts poses, which ruins the
splat. Poll `ProcessInfo.processInfo.thermalState`:

- `.nominal` / `.fair` — full split as above.
- `.serious` — suspend preview training immediately. Capture and streaming continue.
- `.critical` — drop keyframe rate to 2/s, notify the operator, keep streaming.

Capture and upload are never shed. They are the only irreplaceable work.

---

## 3. Milestones

Build strictly in order. Do not start a milestone until the previous gate passes.

### M1 — Capture + on-device processing

Fork the capture path from the reference iOS repo. Keep the ARSession, frame selection,
pose/intrinsics writer, and LiDAR sampling. Keep `msplat` and `MetalSplatter` — unlike a
pure-upload design, we still need both for the preview path in M5.

Keyframe acceptance rules:

- Reject unless the camera has moved ≥ 15 cm **or** rotated ≥ 10° since the last
  accepted frame.
- Reject if the variance-of-Laplacian blur score is below the running median of
  accepted frames. Compute on a downscaled grayscale copy, not the full frame.
- Reject unless `ARFrame.camera.trackingState == .normal`.
- Cap at 300 keyframes per session. Room-scale needs ~150–250.

Camera configuration, applied before the session starts and asserted every frame:

- Lock exposure and white balance. Auto-exposure drift while panning past a window is
  the single most common cause of blotchy splats.
- Fixed focus. Autofocus changes intrinsics mid-capture.
- Record a warning in session metadata if either lock is lost.

LiDAR fusion: accumulate `sceneDepth` into one global point cloud, voxel-downsample at
2 cm, cap at 500k points. Bias retention toward planar low-texture regions — walls and
ceilings are where photometric training has no gradient and the depth prior does the
real work. Never transmit per-frame depth maps.

Ship a **record-to-disk debug mode** that writes a complete `.roomsplat` package
(schema in §6) with no network. This is the fixture every later milestone debugs
against. It is not the shipping path but it must not bit-rot — keep it working.

**Gate M1:** debug mode produces a `.roomsplat` package matching §6, transferable via
the Files app. `transforms.json` validates against the schema; `points3D.ply` opens in
MeshLab as a recognizable room.

### M2 — Offline correctness gate

Copy an M1 debug package to the 5090 box by hand. Train it with plain `gsplat` at
default settings. No network, no streaming, no accelerated backend.

This milestone exists solely to isolate coordinate-system bugs before streaming hides
them. Do not skip it because streaming is the real design.

```bash
cd server
uv run python -m train.run \
  --data /data/captures/<uuid>.roomsplat \
  --init-ply points3D.ply \
  --backend gsplat \
  --out /data/models/<uuid>/
```

**Gate M2:** trained PLY renders from a held-out capture pose, correctly oriented, at
correct metric scale. Measure a known object in the splat (a door is ~2.0 m) and
confirm within 5%. If the room is mirrored or inside-out, go to §5 — do not tune
hyperparameters.

### M3 — Desktop webserver + live ingest

FastAPI on the 5090 box, single origin, three responsibilities:

1. Serve the built `web/` bundle as static files.
2. `/ws/ingest` — binary WebSocket, phone → server.
3. `/ws/viewer` — JSON WebSocket, server → browser, for progress and asset
   notifications.

Single origin means no CORS configuration anywhere in this project. If you find
yourself writing CORS middleware, the topology is wrong.

**Ingest protocol.** One WebSocket carrying both control and payload. Do not split
metadata onto WS and images onto HTTP — with continuous streaming, two channels means
two orderings and you will spend a day on a race condition.

- Text frames: JSON control messages. `session_open`, `keyframe_meta`,
  `capability_report`, `thermal_state`, `session_complete`, `session_abort`.
- Binary frames: `[4-byte frame_index][JPEG bytes]`. Each binary frame is preceded by
  its `keyframe_meta` text frame carrying the pose, intrinsics, and timestamp.
- The fused LiDAR cloud is sent as a binary frame after `session_open` and resent
  incrementally every 50 keyframes as it densifies.

**Backpressure.** Capture can outrun upload on congested wifi. The client watches
`bufferedAmount` and, when it exceeds 8 MB, raises the keyframe acceptance thresholds
(pose delta and rotation) rather than queueing. Dropping a redundant frame is free;
falling behind is not. Log every threshold escalation.

Budget for sanity: ~400 KB per keyframe at 1600 px, ~5 keyframes/s at walking pace,
so ~16 Mbps sustained and ~100 MB for a 250-frame room. If you cannot hit this on LAN,
the bug is in chunking or in a Nagle/`bufferedAmount` interaction, not in bandwidth.

**Disk mirror (required).** Every ingested session is written to disk as a
`.roomsplat` package *as it streams*, byte-identical in structure to the M1 debug
output. Streaming otherwise destroys the inspectable artifacts that make this pipeline
debuggable, and a live session that only exists in memory cannot be replayed. Add a
`--replay <package>` mode to the server that feeds a recorded package through the
ingest path at capture speed. You will use it constantly.

**Gate M3:** walk a room; a complete `.roomsplat` lands on the server with no manual
file movement, and `--replay` on it reproduces the same server-side state.

### M4 — Progressive training + live viewer

This is the centerpiece of the project. The browser must show the room building itself
while the operator walks. Everything else is in service of this.

**Separate geometry latency from appearance latency.** This is the key design decision.
LiDAR geometry is available within milliseconds of capture; trained appearance takes
tens of seconds. Do not make the viewer wait on training to feel alive. The viewer
renders three layers:

| Layer | Source | Latency | Purpose |
|---|---|---|---|
| Point cloud | Fused LiDAR, streamed incrementally | < 500 ms | Immediate sense of the room's shape |
| Trajectory + frustum | Live ARKit pose from `keyframe_meta` | < 200 ms | Shows where the operator is right now |
| Splat cells | Progressive training output | 2–5 s per cell | The actual photorealistic result |

Splat cells fade in over the point cloud as they arrive. The point cloud stays visible
underneath in regions not yet covered by a trained cell, so there is never a hole where
the operator has already walked.

**Spatial chunking.** Partition the scene into a 1 m³ voxel grid in world space, using
the LiDAR cloud's bounds. Each occupied cell is trained, exported, and delivered
independently as its own SPZ, and loaded as its own Spark `SplatMesh`. A full-scene
re-transfer per checkpoint is not acceptable — a 30 MB reload every few seconds
saturates the link and hitches the render. Typical per-cell payload is 200–600 KB.

Gaussian sets are unordered and splat indices are unstable across densification and
pruning, so do not attempt per-splat deltas. The cell is the unit of change.

**Update cadence is wall-clock, not iteration count.** Export every 2 s regardless of
how many iterations completed. Iteration-based cadence makes the update rate depend on
scene size, so the viewer gets choppier exactly as the room gets more interesting.

**Which cells to re-export.** Track a per-cell dirty score: sum of parameter deltas for
splats in that cell since its last export, normalized by splat count. Export the top-N
dirtiest cells each tick, N chosen to keep the export budget under ~1.5 MB per tick.
Cells that have converged stop being re-sent, which is what makes the bandwidth
sustainable over a long walkthrough.

**View sampling during progressive training.** New keyframes are added to the training
set as they land — never restart training on arrival. But uniform sampling over a
growing set means newly-scanned regions converge more slowly the longer you walk, which
is backwards from what the operator expects. Sample 70% from the most recent 20
keyframes and 30% uniformly across all keyframes. The recent bias puts gradient where
the new data is; the uniform tail prevents earlier regions from degrading.

**Manifest protocol.** The server maintains a session manifest and pushes diffs over
`/ws/viewer`:

```json
{
  "type": "manifest_update",
  "session_id": "uuid",
  "tick": 47,
  "cells": [
    {"id": "c_12_3_-4", "url": "/assets/<uuid>/cells/c_12_3_-4.v9.spz",
     "version": 9, "bounds": [12,3,-4,13,4,-3], "splats": 18422}
  ],
  "removed": [],
  "point_cloud_url": "/assets/<uuid>/cloud.v12.bin",
  "live_pose": [[1,0,0,1.2],[0,1,0,1.6],[0,0,1,-3.4],[0,0,0,1]],
  "coverage": {"c_9_3_-4": 1, "c_10_3_-4": 2}
}
```

Cell URLs are versioned and immutable, so they cache normally and the browser fetches
them over plain HTTP in parallel rather than over the WebSocket. The WS carries only
notification. `coverage` is the per-cell distinct-viewpoint count from M5 — the viewer
tints under-covered cells so a second person watching the screen can direct the
operator.

**Viewer-side rules.**

- Fetch cell updates newest-first and never queue: if a newer version of a cell arrives
  while an older one is still parsing, abandon the older fetch.
- Keep the current `SplatMesh` for a cell mounted until its replacement has finished
  parsing, then cross-fade over ~200 ms and dispose the old one. Swapping on
  load-start gives a black hole in the scene on every update.
- Cap concurrent parses at 2. SPZ parsing is main-thread work in most paths; more
  concurrency stalls the render loop, which is the one thing that makes this feel broken.
- Two camera modes: `follow`, where the browser camera tracks the live ARKit pose so
  the screen shows what is being scanned, and `free`, a standard orbit. Default to
  `follow` while a session is open and switch to `free` on `session_complete`.

**Known risk to benchmark, not assume.** Spark sorts splats per frame. Many small
`SplatMesh` instances may cost more than one large one. Measure with 20, 60, and 120
cells before committing to 1 m³; if the sort cost dominates, increase cell size to 2 m³
and accept larger per-cell payloads. Record the result in `web/README.md`.

**Gate M4:** operator walks an unseen room while a browser on the same network shows:
point cloud within 1 s of the operator entering a region, live frustum tracking their
position, and trained splat cells fading in behind them. No visible hitch in the render
loop during updates. Sustained update bandwidth stays under 1 MB/s after the first
30 seconds.

### M5 — On-device preview + coverage feedback

Now use the phone's compute. Train a coarse splat locally with `msplat` at a 1k
target and render it with `MetalSplatter` in a picture-in-picture panel, so the
operator sees geometry forming in-hand without waiting on the desktop.

Layer coverage analysis on top: from the fused LiDAR cloud and accepted camera poses,
compute which surfaces have been observed from fewer than 3 distinct viewpoints and
highlight them in the AR overlay. This is the highest-value thing the phone can compute,
because it is the only feedback that changes what the operator does next.

Implement the thermal governor from §2 in this milestone, not later. Preview training is
what makes thermal shedding necessary.

**Gate M5:** preview splat appears on device within 45 s of capture start; coverage
overlay correctly flags a deliberately under-scanned corner; forcing `.serious` thermal
state suspends preview training without interrupting the stream.

### M6 — Speed and simplification

Only now optimize.

Swap the training backend behind `--backend {gsplat,fastgs,fastergs}`. FastGS is the
aggressive option (claims SOTA results within 100 s; wraps vanilla 3DGS, Scaffold-GS,
and Mip-splatting rather than replacing them). Faster-GS is the conservative option
(~4× faster, ~30% less VRAM, unchanged quality and Gaussian count). Benchmark both on
three recorded sessions via `--replay` so the comparison is against identical input.

Add NanoGS as a post-training simplification pass. It is training-free: load the PLY,
set a target ratio and merge cap, get a smaller PLY back. Use the Python implementation
server-side (`saliteta/NanoGS`) — the browser app's KD-tree is unreliable above ~5M
splats and there is no reason to do this work in the browser.

Convert the simplified result to SPZ for final delivery. Target under 30 MB.

**Gate M6:** wall clock from `session_complete` to final-quality splat in the browser is
under 90 s, and a side-by-side against the M2 baseline shows no obvious regression.

### M7 — Quality passes

Only for problems actually observed:

- **Ghosted/doubled walls** → ARKit pose drift over the room loop. Fix with a
  bundle-adjustment-only COLMAP pass using ARKit poses as fixed initialization. Fast
  because it skips feature matching from scratch. Sole permitted COLMAP usage.
- **Floaters behind windows and mirrors** → capture-time masking UI, or a server-side
  crop box derived from the LiDAR cloud's dominant planes.
- **Mushy walls** → the depth seed is under-weighted or over-decimated. Tune §3 voxel
  retention before touching training hyperparameters.

### M8 — Shaders

Spark exposes per-splat modifiers over position and RGBA. Implement dissolve, tint, and
progressive reveal against that API. Do not drop to raw WebGL. Do not port krpano's
shader code — it is a different renderer with a different splat pipeline; match the
*effect*, not the implementation.

---

## 4. Streaming and live-update invariants

These hold at every milestone from M3 onward:

- **Every streamed session is also a file on disk.** No exceptions, no flag to disable.
- **The disk format is identical to the debug-mode format.** One schema, one parser,
  one set of tests.
- **Every stage is replayable.** `--replay` must reproduce a session end-to-end.
- **The stream is lossy by design and the pipeline is fine with it.** A dropped frame
  is a logged warning. The server trains on what arrived. Never block capture on
  acknowledgment.
- **Reconnect resumes, never restarts.** The client tracks the highest acknowledged
  `frame_index` and resumes from there on reconnect. A dropped wifi association must
  not cost a walkthrough.

From M4 onward, the viewer side adds:

- **The viewer is a pure consumer of the manifest.** It holds no derived state the
  server cannot rebuild. A browser refresh mid-session recovers the full current view
  from the latest manifest and cached cell URLs, with no replay of history.
- **Updates are idempotent and order-independent.** Manifest diffs may arrive out of
  order or be missed entirely. Each carries absolute cell versions, not increments, and
  the viewer applies only versions newer than what it holds.
- **Never block the render loop on an update.** Dropping an intermediate cell version is
  always correct — the next tick supersedes it. A stalled frame is never correct.
- **The point cloud layer is never removed during a session,** only occluded by trained
  cells. It is the guarantee that the room always looks continuous even when training
  lags behind the walk.
- **A late-joining browser sees the current state, not the beginning.** Opening the
  viewer 90 seconds into a session gets the full manifest immediately.

---

## 5. Coordinate conventions — read this twice

This is where the time goes. Three conventions are in play:

| System | Handedness | Up | Camera looks |
|---|---|---|---|
| ARKit | right | +Y | −Z |
| COLMAP | right | −Y | +Z |
| Nerfstudio `transforms.json` (OpenGL/Blender) | right | +Y | −Z |

We write Nerfstudio convention, because that is what the reference dataset writer
produces and what gsplat's parser expects.

**Port the transform from `vendor/ios-gaussian-splatting-demo/3DGS Demo/Capture/`
verbatim.** It already resolves ARKit → Nerfstudio correctly. Do not re-derive it from
the table above. Do not clean it up. Copy it, comment the source file, and write a unit
test pinning the output matrix for a known input pose.

Diagnostic table — if you see this, it is this:

| Symptom | Cause |
|---|---|
| Training converges, room is mirrored | Handedness flip in the pose matrix |
| Camera appears inside geometry looking out | Camera-forward sign flip |
| Smeared blob, loss plateaus high | Poses and images misaligned by index — with streaming, suspect the binary/text frame pairing first |
| Correct shape, wrong scale | Unit conversion; ARKit is already metric, do not rescale |

Never debug these by tuning learning rates.

---

## 6. Data contract

The `.roomsplat` package is the interface between iOS and server, and the disk mirror
format for live sessions. Both sides validate it.

```
<uuid>.roomsplat/
├── capture.json          # our metadata (see below)
├── transforms.json       # Nerfstudio-style camera model + frames
├── thumbnail.jpg
├── images/
│   ├── frame_00000.jpg   # 1600 px long edge, quality 0.85
│   └── ...
└── points3D.ply          # fused LiDAR cloud, voxel-downsampled, XYZRGB
```

`transforms.json` — top level carries `camera_model`, `fl_x`, `fl_y`, `cx`, `cy`, `w`,
`h`. `frames` is an array of `{ file_path, transform_matrix }` where the matrix is 4×4
camera-to-world, row-major. This matches the reference repo's writer exactly; keep it
that way so vendored parsers keep working. During a live session the server appends to
this file incrementally and must leave it valid JSON after every append.

`capture.json`:

```json
{
  "schema_version": 2,
  "session_id": "uuid",
  "device_model": "iPhone17,2",
  "captured_at": "2026-08-22T10:14:00Z",
  "source": "stream",
  "frame_count": 218,
  "frames_dropped": 3,
  "exposure_locked": true,
  "white_balance_locked": true,
  "tracking_warnings": [],
  "thermal_events": [{"t": 142.5, "state": "serious"}],
  "backpressure_events": 2,
  "preview_trained_on_device": true,
  "seed_point_count": 412883,
  "seed_voxel_size_m": 0.02
}
```

`source` is `"stream"` or `"debug"`. Bump `schema_version` on any breaking change and
make the server reject unknown versions loudly rather than guessing.

---

## 7. Environment

- **Server:** Linux, RTX 5090. Python via `uv`, FastAPI + uvicorn. Pin CUDA and torch
  versions in `pyproject.toml` and record the working combination in
  `server/README.md` — gsplat and the accelerated forks are sensitive to this and a
  working pin is worth more than a flexible range.
- **iOS:** iOS 18+, physical device required (Simulator cannot validate ARKit capture,
  LiDAR, thermal state, or on-device training). Swift 6.1 package support.
- **Web:** Vite, three.js, `@sparkjsdev/spark`. Served by the desktop, same origin as
  both WebSockets.
- Assume phone and server share a Tailscale tailnet. Do not hardcode IPs; read the
  server host from app settings and support mDNS discovery on the local subnet as a
  convenience.

---

## 8. Working agreements

- Commit at every gate, tagged `m1`…`m8`. A gate commit includes its passing test.
- Keep at least three recorded sessions in `fixtures/` and run them through `--replay`
  in CI. Every performance claim in this project must be measured against identical
  recorded input, never against a fresh walkthrough.
- Server logs one structured line per pipeline stage with wall-clock duration, from M2
  onward. You cannot optimize M6 without this history.
- When a reference repo and this spec disagree on a detail not covered here, follow the
  reference repo and note the decision in `AGENTS.md`.
- Do not add dependencies not named in this document without flagging it first.
