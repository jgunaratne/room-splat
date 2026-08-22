# RoomSplat server

Runs on the 5090 box (SPEC.md §7). FastAPI + uvicorn host the built web bundle and
both WebSockets; the training driver runs gsplat.

## Layout

- `roomsplat/` — shared library: the `.roomsplat` data contract (§6), ingest wire
  protocol (§3), coordinate transform (§5), spatial chunking + manifest (§4).
- `ingest/` — live session, disk mirror.
- `app/` — FastAPI app (`main.py`), session manager, `--replay` driver.
- `train/` — backend abstraction, progressive trainer, exporters, offline `run.py`.
- `simplify/` — NanoGS invocation (M6).

## Install & run (GPU-free core)

```bash
pip install -e .            # or: pip install -e '.[dev]' for tests
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m pytest            # 13 tests, no GPU required
```

The default install has no torch/gsplat, so the whole pipeline — ingest, disk mirror,
chunking, manifest, replay, viewer host — runs and is tested without CUDA. Live
sessions then train with the GPU-free `synthetic` backend (deterministic, not
photorealistic; it exercises the export/manifest path end to end).

## Training on the GPU (M2 gate)

```bash
pip install -e '.[train]'
ROOMSPLAT_BACKEND=gsplat uvicorn app.main:app --host 0.0.0.0 --port 8000
# offline correctness gate:
python -m train.run --data /data/captures/<uuid>.roomsplat \
  --init-ply points3D.ply --backend gsplat --out /data/models/<uuid>/
```

**Pin the working combination.** gsplat is sensitive to torch/CUDA (SPEC.md §7). The
ranges in `pyproject.toml` `[train]` are a starting point; once M2 passes on this box,
record the exact torch/CUDA/gsplat versions here:

| torch | CUDA | gsplat | M2 status |
|---|---|---|---|
| — | — | — | not yet validated on this box |

## Replay (M3 gate, §8)

```bash
python -m app.replay --package data/captures/<uuid>.roomsplat --speed 1
```

Feeds a recorded package through the same SessionManager the live socket uses, so it
reproduces server-side state. Every performance claim is measured against recorded
input via this path, never a fresh walkthrough.

## Environment variables

- `ROOMSPLAT_DATA` — capture mirror dir (default `data/captures`)
- `ROOMSPLAT_ASSETS` — viewer asset dir (default `data/assets`)
- `ROOMSPLAT_WEB` — built web bundle to serve at `/` (default `../web/dist`)
- `ROOMSPLAT_BACKEND` — `synthetic` (default) | `gsplat` | `fastgs` | `fastergs`
