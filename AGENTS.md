# AGENTS.md — roles + handoff protocol

Three surfaces, built strictly in milestone order (SPEC.md §3). A milestone is done
when its gate test passes, not when the code looks right. Commit at every gate, tagged
`m1`…`m8`, with the passing test in the commit.

## Handoff protocol

- The `.roomsplat` package (SPEC.md §6) is the contract between iOS and server. Both
  sides validate it through the **one** parser in `server/roomsplat/package.py`. If you
  change the schema, bump `schema_version` and make the server reject unknown versions
  loudly.
- The streaming disk mirror is byte-identical in structure to the debug package, so
  `--replay` reproduces any session and every stage is debuggable offline.
- When a reference repo and the spec disagree on a detail not covered by the spec,
  follow the reference and note it below (SPEC.md §8).

## Decisions taken during implementation

1. **Shared library location.** SPEC.md §1 lists `app/`, `ingest/`, `train/`,
   `simplify/`. Code shared across those (the data contract, wire protocol, coordinate
   transform, chunking, manifest) lives in a fifth package `server/roomsplat/` so there
   is exactly one parser/one schema (§4). The four spec packages import from it.

2. **`camera_model` is `OPENCV`.** The reference writer emits `camera_model: "OPENCV"`
   with ARKit intrinsics; we keep it verbatim so vendored parsers keep working (§5, §8).
   SPEC.md §6 leaves the value to the reference, so this follows the reference.

3. **GPU-free `synthetic` backend + real gsplat backend.** The real `gsplat` backend
   (`train/gsplat_trainer.py`) trains on the 5090 (rasterizer + DefaultStrategy
   densification, full-SH PLY export). The deterministic `synthetic` backend
   (`train/backends.py`) keeps the ingest/chunk/export/manifest/viewer pipeline runnable
   and tested without CUDA; it is a test/CI aid, never a deliverable. The gsplat GPU
   loop and coordinate conversion are validated by `tests/test_gsplat_gpu.py`
   (auto-skips without CUDA). The trainer loads keyframes incrementally by re-reading
   the disk mirror each tick (`refresh()`), so live streaming trains with gsplat without
   restarting (§4), using 70/30 recent/uniform view sampling. The remaining M2 work is
   the metric-scale/orientation check against a real room capture (a door ~2.0 m within
   5%). Set `ROOMSPLAT_BACKEND=gsplat` to use it live/offline.

4. **Cell asset format: PLY now, SPZ on the GPU box.** SPEC.md §4/M6 specify per-cell
   SPZ. The GPU-free export path writes 3DGS PLY (Spark loads both) and the manifest's
   `asset_ext` is `ply`; the 5090 path flips it to `spz` after the M6 NanoGS→SPZ step.
   The manifest, viewer, and cache semantics are identical either way.

5. **Point-cloud wire/asset format.** A trivial `[uint32 count][xyzrgb…]` binary
   (`train/export.py`) is used both on the ingest wire (LiDAR cloud frame) and as the
   viewer asset (`cloud.v<n>.bin`), so there is one format to parse. `points3D.ply` in
   the package stays binary XYZRGB per §6.

## Open items (need hardware, tracked against milestones)

- **M2:** gsplat training wired + GPU convergence gate passing; torch/CUDA/gsplat
  pinned in `server/README.md`. Remaining: metric-scale gate on a real room capture.
- **M4:** run the 20/60/120-cell Spark sort benchmark; record in `web/README.md`.
- **M5:** on-device preview (msplat/MetalSplatter) + coverage overlay on a real device.
- **M6:** FastGS/Faster-GS comparison via `--replay`; NanoGS + SPZ under 30 MB.
- **M7:** COLMAP BA-only pass (sole permitted COLMAP usage) if walls ghost.
- **M8:** Spark per-splat shader modifiers (dissolve, tint, progressive reveal).
- Keep ≥3 recorded sessions in `fixtures/` and run them through `--replay` in CI (§8).
