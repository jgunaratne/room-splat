# RoomSplat

Capture a room with an iPhone Pro (ARKit + LiDAR), stream it live to a desktop with an
RTX 5090, train a 3D Gaussian Splat progressively, and render it in the browser with
three.js + Spark. See [SPEC.md](../SPEC.md) for the full design.

## Packages

| Dir | What | Status |
|---|---|---|
| `server/` | FastAPI ingest + disk mirror + progressive training + viewer host | M2–M4 core implemented & tested (GPU-free); gsplat/NanoGS wired but need the 5090 |
| `web/` | Vite + three.js + Spark viewer | M4 three-layer viewer implemented |
| `ios/` | ARKit capture, streaming, thermal governor | spec-critical Swift ported; full Xcode app pending a device |

## Quick start (no GPU, no phone)

```bash
cd server && pip install -e '.[dev]' && python -m pytest    # 13 passing
# run a recorded/synthetic session through the whole server pipeline:
python -m app.replay --package <a .roomsplat> --speed 1
# serve the viewer + sockets:
uvicorn app.main:app --port 8000
cd ../web && npm install && npm run dev
```

The GPU-free path uses a deterministic `synthetic` training backend so the full
ingest → chunk → export → manifest → viewer loop runs and is tested without CUDA.
Swap `ROOMSPLAT_BACKEND=gsplat` on the 5090 box for real training.

## What's done vs. what needs hardware

Implemented and tested here: the `.roomsplat` data contract (§6), the single-socket
ingest protocol + disk mirror (M3), `--replay` (M3/§8), spatial chunking + dirty
scoring + wall-clock export (§4), the manifest diff protocol (§4), coverage counting
(§4), the ported ARKit→Nerfstudio transform with a pin test (§5), and the three-layer
web viewer (M4).

Needs the RTX 5090: real gsplat/FastGS/Faster-GS training (M2/M6), NanoGS
simplification and SPZ conversion (M6), COLMAP bundle-adjustment pass (M7). Needs a
physical iPhone: the full ARKit capture app, on-device preview, coverage overlay (M1,
M5). See per-package READMEs and `AGENTS.md` for the details and open decisions.
