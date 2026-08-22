# RoomSplat viewer

Vite + three.js + `@sparkjsdev/spark`. Served by the desktop at the same origin as
both WebSockets (SPEC.md M3), so there is no CORS anywhere.

```bash
npm install
npm run dev      # dev server, proxies /ws and /assets to localhost:8000
npm run build    # emits dist/, which the FastAPI server mounts at /
```

## Layers (SPEC.md §4)

| Layer | Module | Source |
|---|---|---|
| Point cloud | `src/pointcloud.js` | `cloud.v<n>.bin` over HTTP |
| Trajectory + frustum | `src/frustum.js` | `live_pose` in the manifest |
| Splat cells | `src/cells.js` | per-cell `.spz`/`.ply` over HTTP |

The WebSocket (`/ws/viewer`) carries only manifest notifications; assets are fetched
over plain HTTP from versioned, immutable URLs. The viewer holds no state the server
cannot rebuild, so a refresh recovers from the next manifest snapshot.

## Cell-size benchmark (SPEC.md M4 — required, not yet run)

Spark sorts splats per frame, so many small `SplatMesh` instances may cost more than
one large one. Benchmark 20 / 60 / 120 cells before committing to 1 m³; if sort cost
dominates the frame, raise the server's `cell_size` to 2 m³ and accept larger payloads.

**Status:** not yet measured — requires a recorded session and the Spark runtime on a
real GPU. Record results in this table when run:

| Cells | Frame time (ms) | Sort cost (ms) | Verdict |
|---|---|---|---|
| 20 | — | — | — |
| 60 | — | — | — |
| 120 | — | — | — |
