"""FastAPI app on the 5090 box (SPEC.md M3).

Single origin, three responsibilities: serve the built web/ bundle, accept the
binary ingest WebSocket, and push JSON manifests to viewers. Single origin means
there is no CORS middleware anywhere — if you reach for it, the topology is wrong.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ingest.session import LiveSession
from roomsplat.protocol import (
    ControlType,
    KeyframeMeta,
    decode_binary,
    is_point_cloud,
    is_preview,
)
from train.export import read_point_cloud_bin

from .manager import SessionManager

log = logging.getLogger("roomsplat.app")

# Make our roomsplat.* logs visible: uvicorn only attaches handlers to its own
# loggers, so per-stage lines (session_open, train, export) would otherwise vanish.
_rs = logging.getLogger("roomsplat")
if not _rs.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _rs.addHandler(_h)
    _rs.setLevel(logging.INFO)
    _rs.propagate = False

DATA_DIR = Path(os.environ.get("ROOMSPLAT_DATA", "data/captures"))
ASSETS_DIR = Path(os.environ.get("ROOMSPLAT_ASSETS", "data/assets"))
WEB_DIST = Path(os.environ.get("ROOMSPLAT_WEB", "../web/dist"))


def create_app(manager: SessionManager | None = None) -> FastAPI:
    app = FastAPI(title="RoomSplat")
    app.state.manager = manager or SessionManager(DATA_DIR, ASSETS_DIR)

    @app.websocket("/ws/ingest")
    async def ws_ingest(ws: WebSocket):
        await ws.accept()
        mgr: SessionManager = app.state.manager
        current: str | None = None
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                # The stream is lossy by design (SPEC.md §4): a malformed or
                # unexpected frame is logged and skipped, never fatal to the socket.
                try:
                    if (text := msg.get("text")) is not None:
                        current = await _handle_control(mgr, ws, json.loads(text), current)
                    elif (data := msg.get("bytes")) is not None:
                        await _handle_binary(mgr, ws, current, data)
                except WebSocketDisconnect:
                    raise
                except Exception:
                    log.exception("ingest frame handling failed (session=%s); dropping frame", current)
        except WebSocketDisconnect:
            pass
        log.info("ingest socket closed (session=%s)", current)

    @app.websocket("/ws/viewer")
    async def ws_viewer(ws: WebSocket):
        await ws.accept()
        mgr: SessionManager = app.state.manager
        await mgr.add_viewer(ws)
        try:
            while True:
                # The viewer is a pure consumer except for one control: a Reset request,
                # which drops the current session(s) + assets so a new capture starts
                # from scratch (server broadcasts "reset" so every viewer clears too).
                text = await ws.receive_text()
                try:
                    if json.loads(text).get("type") == "reset":
                        await mgr.reset()
                except (ValueError, AttributeError):
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            mgr.remove_viewer(ws)

    @app.get("/live/{session_id}.jpg")
    async def live_frame(session_id: str):
        # Latest keyframe JPEG for the picture-in-picture panel. Kept in memory and
        # marked no-store since it changes every frame (cache-bust via ?t=frame_index).
        rt = app.state.manager.sessions.get(session_id)
        jpeg = rt.session.latest_jpeg if rt else None
        if not jpeg:
            return Response(status_code=404)
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # /assets serves versioned, immutable cell + cloud URLs referenced by manifests.
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")

    return app


async def _handle_control(mgr: SessionManager, ws: WebSocket, msg: dict, current: str | None) -> str | None:
    mtype = msg.get("type")
    if mtype == ControlType.SESSION_OPEN:
        rt = await mgr.open_session(msg)
        # Tell the client where we are so a reconnect resumes from the next frame; for a
        # fresh session highest_ack is -1 and the client starts at 0 (SPEC.md §4).
        await ws.send_text(json.dumps({"type": ControlType.ACK, "frame_index": rt.session.highest_ack}))
        return rt.session.session_id
    rt = mgr.sessions.get(current) if current else None
    if rt is None:
        log.warning("control %s before session_open", mtype)
        return current
    session: LiveSession = rt.session
    if mtype == ControlType.KEYFRAME_META:
        meta = KeyframeMeta.from_msg(msg)
        session.on_keyframe_meta(meta)
        rt.on_keyframe_pose(meta.transform_matrix)
    elif mtype == ControlType.THERMAL_STATE:
        session.on_thermal_state(float(msg.get("t", 0.0)), str(msg.get("state", "")))
    elif mtype == ControlType.TRACKING_WARNING:
        session.on_tracking_warning(str(msg.get("message", "")))
    elif mtype == ControlType.CAPABILITY_REPORT:
        log.info("stage=capability session=%s report=%s", current, msg)
        # Server determines stage assignment based on phone capabilities (SPEC.md §2):
        # Full training and chunking always run on desktop; preview training is enabled
        # on device only if supported and server doesn't withhold it.
        assignment = {
            "type": ControlType.STAGE_ASSIGNMENT,
            "preview_training": bool(msg.get("preview_training_supported", False)),
            "coverage_analysis": True,
            "keyframe_selection": True,
            "lidar_fusion": True,
        }
        await ws.send_text(json.dumps(assignment))
    elif mtype == ControlType.SESSION_COMPLETE:
        await mgr.close_session(current)
        return None
    elif mtype == ControlType.SESSION_ABORT:
        await mgr.close_session(current)
        return None
    return current


async def _handle_binary(mgr: SessionManager, ws: WebSocket, current: str | None, data: bytes) -> None:
    rt = mgr.sessions.get(current) if current else None
    if rt is None:
        log.warning("binary frame before session_open")
        return
    frame_index, payload = decode_binary(data)
    if is_point_cloud(frame_index):
        xyz, rgb = read_point_cloud_bin(payload)
        version = rt.session.on_point_cloud(xyz, rgb)
        rt.on_point_cloud(xyz, rgb, version)
        await mgr.broadcast_log(f"LiDAR cloud v{version}: {len(xyz):,} points")
    elif is_preview(frame_index):
        # Timer-driven preview: refresh the PiP only. Not mirrored, not trained, not
        # acked (best-effort, so it never competes with keyframes on a congested link).
        seq = rt.session.on_preview(payload)
        await mgr.broadcast({
            "type": "live_frame", "session_id": rt.session.session_id, "frame_index": seq,
        })
    else:
        if rt.session.on_image(frame_index, payload) is not None:
            rt.on_image()
            # Notify viewers there's a fresh frame to show in the PiP panel; the image
            # itself is fetched over HTTP from /live/<id>.jpg (WS carries only notice).
            await mgr.broadcast({
                "type": "live_frame",
                "session_id": rt.session.session_id,
                "frame_index": rt.session.live_seq,
            })
        # Ack every image that is now durably on disk. The client advances its resume
        # point to this index; capture is never blocked on the ack (SPEC.md §4).
        await ws.send_text(json.dumps({"type": ControlType.ACK, "frame_index": frame_index}))


app = create_app()
