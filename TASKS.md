# TASKS.md — parallel work board

Checklist for multiple coding agents working at once. Pairs with `SPEC.md` (the what)
and `AGENTS.md` (decisions + handoff). Build in milestone order; **do not start a
milestone until the previous gate passes** (SPEC.md §3).

## How to use this board

- **Claim before you code.** Change `- [ ]` to `- [~]` and append `— @your-handle,
  YYYY-MM-DD` so others don't collide. Mark `- [x]` only when the task's gate/test
  passes, and append the commit hash.
- **Stay in your lane.** Each task lists an *Area* (file/dir). Two agents must not edit
  the same Area at once. If a task needs cross-area changes, split it or coordinate in
  `AGENTS.md`.
- **A task is done when its check passes**, not when the code looks right. Every task
  states its *Done when*.
- **Gates are blocking.** Tasks under a locked milestone (⛔) may be drafted but not
  merged until the gate above them is green.

Legend: `[ ]` open · `[~]` in progress · `[x]` done · `[b]` blocked (note why).

---

## Track A — Server (Python, `server/`)

### M2/M3/M4 — already landed (verify, don't redo)

- [x] Data contract `.roomsplat` reader/writer/validator — `server/roomsplat/package.py` (f58379f)
- [x] Ingest wire protocol + single-socket handler + disk mirror — `ingest/`, `app/main.py` (f58379f)
- [x] `--replay` reproduces server state — `app/replay.py` (f58379f)
- [x] Spatial chunking + dirty scoring + wall-clock export — `roomsplat/chunking.py`, `train/progressive.py` (f58379f)
- [x] Manifest diff protocol + coverage counting — `roomsplat/manifest.py` (f58379f)
- [x] Coordinate transform + pin/projection tests — `roomsplat/coords.py`, `tests/test_coords.py` (4abb8b8)
- [x] Real gsplat backend + GPU convergence gate — `train/gsplat_trainer.py`, `tests/test_gsplat_gpu.py` (4abb8b8)
- [x] Incremental live gsplat training (no restart) — `train/gsplat_trainer.py#refresh` (8745654)

### M2 — offline correctness gate (needs real capture data)

- [ ] **Metric-scale/orientation gate.** Train a real room capture with `--backend gsplat`;
  measure a known object (door ≈ 2.0 m) in the result within 5%; confirm upright, not
  mirrored/inside-out. *Area:* `train/`, `fixtures/`. *Done when:* a documented
  measurement in `server/README.md` passes and a `fixtures/` capture is committed.
  *Depends on:* a real `.roomsplat` from iOS M1.

### M3 — streaming hardening

- [~] **Server → client acks + resume.** — @jtui, 2026-08-22. Server sends
  `{"type":"ack","frame_index":N}`; document the resume-from-highest-ack contract end to
  end. *Area:* `app/main.py`, `ingest/session.py`. *Done when:* a test drops the socket
  mid-stream and the session resumes without duplicating or losing mirrored frames.
- [~] **Structured stage logging** — @jtui, 2026-08-22. Wall-clock duration for every
  pipeline stage (train tick, export, simplify) — required from M2 on (SPEC.md §8).
  *Area:* `train/`, `app/`. *Done when:* one structured line per stage with `wall_s=`
  appears in logs.

### M6 — speed + simplification (⛔ after M4 gate)

- [ ] **Wire FastGS backend** behind `--backend fastgs`. *Area:* `train/backends.py`,
  new `train/fastgs_trainer.py`. *Done when:* trains a replayed session and exports a PLY.
- [ ] **Wire Faster-GS backend** behind `--backend fastergs`. *Area:* same as above.
- [ ] **Backend benchmark** across 3 recorded sessions via `--replay` (identical input).
  *Area:* `train/`, `server/README.md`. *Done when:* a table of wall-clock + quality is
  recorded in `server/README.md`.
- [ ] **NanoGS simplification pass** (`saliteta/NanoGS`, server-side). *Area:*
  `simplify/nanogs.py`. *Done when:* takes a trained PLY + target ratio → smaller PLY.
- [ ] **SPZ conversion for delivery** (< 30 MB target); flip manifest `asset_ext` to
  `spz` on the GPU path. *Area:* `train/export.py`, `roomsplat/manifest.py`. *Done when:*
  cells serve as `.spz` and the viewer loads them.
- [ ] **M6 gate:** wall clock `session_complete` → final splat in browser < 90 s, no
  obvious regression vs M2 baseline. *Done when:* measured via `--replay` and recorded.

### M7 — quality passes (only for observed problems, ⛔ after M6)

- [ ] **COLMAP bundle-adjustment-only pass** (ARKit poses fixed init) for ghosted walls —
  the *sole* permitted COLMAP usage. *Area:* new `train/refine_poses.py`.
- [ ] **LiDAR-plane crop box** to kill floaters behind windows/mirrors. *Area:* `train/`.

---

## Track B — Web viewer (`web/`)

- [x] Three-layer viewer (point cloud + frustum + cross-fading cells), follow/free camera,
  pure manifest consumer — `web/src/*` (f58379f)

- [ ] **Cell-size benchmark (M4, required).** Measure frame time + Spark sort cost at
  20/60/120 cells; if sort dominates, raise server `cell_size` to 2 m³. *Area:*
  `web/`, `web/README.md`. *Done when:* the results table in `web/README.md` is filled.
- [ ] **Coverage tinting.** The manifest already carries per-cell `coverage`; tint
  under-covered cells so a second person can direct the operator (SPEC.md §4). *Area:*
  `web/src/cells.js`, `web/src/main.js`. *Done when:* under-covered cells render tinted.
- [ ] **Visual verification against a real trained scene** (not synthetic). Confirm
  orientation/scale look right in-browser. *Area:* `web/`. *Depends on:* Track A M2 data.
- [ ] **M8 shaders** (⛔ after M6): dissolve, tint, progressive reveal via Spark's
  per-splat position/RGBA modifiers — do not drop to raw WebGL. *Area:* new
  `web/src/effects.js`. *Done when:* each effect runs on a loaded cell.

---

## Track C — iOS (`ios/`, needs Xcode + a physical device)

- [x] Coordinate transform ported verbatim + notices — `Capture/CoordinateTransform.swift`
- [x] Keyframe selector (M1 rules + backpressure) — `Capture/KeyframeSelector.swift`
- [x] Single-socket ingest client — `Stream/IngestClient.swift`
- [x] Thermal governor — `Governor/ThermalGovernor.swift`
- [x] Buildable Xcode project + app entry — `ios/RoomSplat.xcodeproj`, `App/` (0aa55f5)
- [x] ARKit capture flow + capture UI + live link state — `Capture/CaptureCoordinator.swift`, `Views/` (c504c2e, 53b33ff)
- [x] Blur score (variance-of-Laplacian, downscaled) — `Capture/BlurScore.swift`
- [x] Point cloud fuser (initial) — `Capture/PointCloudFuser.swift`

- [ ] **LiDAR fusion to spec:** 2 cm voxel downsample, 500k cap, bias retention toward
  planar low-texture regions, resend cloud every 50 keyframes; never transmit per-frame
  depth. *Area:* `Capture/PointCloudFuser.swift`. *Done when:* fused cloud matches §3
  params and streams incrementally.
- [ ] **Camera locks:** lock exposure + white balance, fixed focus; assert every frame;
  record a warning in session metadata if a lock is lost. *Area:* `Capture/`. *Done when:*
  locks are applied pre-session and `tracking_warnings` records losses.
- [ ] **Debug record-to-disk `.roomsplat` writer (M1 gate).** Writes the §6 package with
  no network, exportable via the Files app. *Area:* new `Capture/PackageWriter.swift`.
  *Done when:* the package validates against `roomsplat/package.py` and `points3D.ply`
  opens in MeshLab as a recognizable room. **This unblocks Track A M2.**
- [ ] **Reconnect resume + real backpressure.** Resume from highest acked `frame_index`;
  raise keyframe thresholds when the socket buffer exceeds 8 MB. *Area:* `Stream/`.
  *Done when:* a dropped wifi association resumes without restarting the walkthrough.
- [ ] **Capability report + thermal_state messages** on the wire; honor server stage
  assignment (no hardcoded split). *Area:* `Stream/`, `Capture/`.
- [ ] **mDNS discovery** of the server on the local subnet (host setting already exists).
  *Area:* `Stream/`, settings UI.

### M5 — on-device preview + coverage (⛔ after M4 gate)

- [ ] **Coarse preview splat** via `msplat` (~1k iters) + **MetalSplatter PiP** panel.
  *Area:* new `Preview/`. *Done when:* a preview appears on device within 45 s of start.
- [ ] **Coverage overlay** in AR: flag surfaces seen from < 3 viewpoints. *Area:*
  new `Preview/CoverageOverlay.swift`. *Done when:* a deliberately under-scanned corner
  is highlighted.
- [ ] **Thermal shedding under load:** `.serious` suspends preview but not the stream;
  `.critical` drops to 2 keyframes/s. *Area:* `Governor/`, `Capture/`, `Preview/`.
  *Done when:* forcing `.serious` suspends preview without interrupting the stream.

---

## Track D — Ops / infra

- [x] nginx vhost + deploy script for `sea.octo80.com` — `deploy/` (a136011)
- [x] Server running + pinned GPU combo — `server/run-server.sh`, `server/README.md` (4abb8b8)

- [ ] **systemd unit** so the server survives reboot (currently a `setsid` process).
  *Area:* `deploy/`. *Done when:* `systemctl enable --now roomsplat` keeps it up across a
  reboot. *Needs sudo.*
- [ ] **CI replay job:** keep ≥ 3 recorded sessions in `fixtures/` and run them through
  `--replay` on every push (SPEC.md §8). *Area:* `.github/workflows/`, `fixtures/`.
  *Done when:* CI is green replaying committed fixtures.
- [ ] **Gate commit tags** `m1`…`m8`, each including its passing test (SPEC.md §8).
  *Area:* repo. *Done when:* tags exist at the corresponding gate commits.

---

## Current critical path

1. iOS **debug `.roomsplat` writer** (Track C) → unblocks
2. Server **M2 metric-scale gate** (Track A) with a real capture → unblocks
3. **M4 gate** end-to-end on a real walk → unblocks M5 (iOS preview) and M6 (server speed).

Everything above the M4 gate can proceed in parallel today; M5/M6/M7/M8 are gated.
