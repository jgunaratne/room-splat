# RoomSplat

Capture a room with an iPhone Pro (ARKit + LiDAR), stream it live to a desktop GPU, train a 3D Gaussian Splat progressively, and watch it sharpen in the browser.

<!-- ![Splat refining in the browser viewer as frames stream in](docs/demo.gif) -->
<!-- TODO: GIF of the viewer while a capture is streaming -->

## How it works

```
iPhone (ARKit + LiDAR) ──single WebSocket──► FastAPI ingest ──► disk mirror (.roomsplat)
  capture, thermal governor                       │
                                                  ▼
                                  progressive trainer (gsplat) ── spatial chunks + dirty scoring
                                                  │
                                       manifest diffs over WS
                                                  ▼
                              Browser viewer (three.js + Spark), three layers
```

- **Data contract:** a `.roomsplat` session package (frames, poses, depth, intrinsics) that the server mirrors to disk, so any capture can be replayed exactly with `app.replay`.
- **Progressive training:** the scene is split into spatial chunks. Chunks with the most new coverage are retrained and exported first, and the viewer receives only manifest diffs.
- **Coordinates:** ARKit poses are converted to Nerfstudio conventions, and a pin test locks the transform.
- **No-GPU path:** a deterministic `synthetic` backend runs the full ingest → chunk → export → viewer loop in CI without CUDA.

## Stack

Swift · ARKit · Python · FastAPI · gsplat · three.js · Spark · Vite

## Run it (no GPU, no phone)

```bash
cd server && pip install -e '.[dev]' && python -m pytest
python -m app.replay --package <session.roomsplat> --speed 1
uvicorn app.main:app --port 8000
cd ../web && npm install && npm run dev
```

For real training, set `ROOMSPLAT_BACKEND=gsplat` on a CUDA machine. The iOS capture app is in `ios/` (open `RoomSplat.xcodeproj`; it requires a LiDAR iPhone). See [SPEC.md](SPEC.md) for milestones and what still needs hardware.

Full setup, configuration and design notes: [GUIDE.md](GUIDE.md).
