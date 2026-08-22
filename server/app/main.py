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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ingest.session import LiveSession
from roomsplat.protocol import (
    ControlType,
    KeyframeMeta,
    decode_binary,
    is_point_cloud,
)
from train.export import read_point_cloud_bin

from .manager import SessionManager

log = logging.getLogger("roomsplat.app")

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
                if (text := msg.get("text")) is not None:
                    current = await _handle_control(mgr, ws, json.loads(text), current)
                elif (data := msg.get("bytes")) is not None:
                    await _handle_binary(mgr, current, data)
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
                await ws.receive_text()  # viewer is a pure consumer; ignore input
        except WebSocketDisconnect:
            pass
        finally:
            mgr.remove_viewer(ws)

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
    elif mtype == ControlType.CAPABILITY_REPORT:
        log.info("stage=capability session=%s report=%s", current, msg)
    elif mtype == ControlType.SESSION_COMPLETE:
        await mgr.close_session(current)
        return None
    elif mtype == ControlType.SESSION_ABORT:
        await mgr.close_session(current)
        return None
    return current


async def _handle_binary(mgr: SessionManager, current: str | None, data: bytes) -> None:
    rt = mgr.sessions.get(current) if current else None
    if rt is None:
        log.warning("binary frame before session_open")
        return
    frame_index, payload = decode_binary(data)
    if is_point_cloud(frame_index):
        xyz, rgb = read_point_cloud_bin(payload)
        version = rt.session.on_point_cloud(xyz, rgb)
        rt.on_point_cloud(xyz, rgb, version)
    else:
        if rt.session.on_image(frame_index, payload) is not None:
            rt.on_image()


app = create_app()
