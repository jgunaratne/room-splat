"""Session manager: owns live sessions, drives training, broadcasts manifests.

Transport-agnostic core shared by the WebSocket handlers (main.py) and --replay
(replay.py). It ties together the ingest disk mirror (LiveSession) and the
progressive trainer, and fans manifest diffs out to connected viewers.

Backend selection is not hardcoded per feature: set ROOMSPLAT_BACKEND=gsplat on the
5090 box; tests and GPU-free runs use "synthetic".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from ingest.session import LiveSession
from roomsplat.manifest import Manifest
from roomsplat.package import CameraModel
from train.backends import make_backend
from train.progressive import TICK_SECONDS, ProgressiveTrainer

log = logging.getLogger("roomsplat.app")

POINT_CLOUD_RESEND_EVERY = 50  # keyframes (§3)


class SessionRuntime:
    def __init__(self, manager: "SessionManager", session: LiveSession, backend_name: str):
        self.manager = manager
        self.session = session
        self.backend_name = backend_name
        self.assets_dir = manager.assets_dir / session.session_id
        self.manifest = Manifest(session_id=session.session_id)
        self.trainer: ProgressiveTrainer | None = None
        self._tick_task: asyncio.Task | None = None
        self._frames_since_cloud = 0

    def _ensure_trainer(self) -> None:
        if self.trainer is not None or self.session.point_cloud is None:
            return
        xyz, rgb = self.session.point_cloud
        backend = make_backend(self.backend_name, seed_xyz=xyz, seed_rgb=rgb,
                               package_root=self.session.root)
        self.trainer = ProgressiveTrainer(
            backend=backend, manifest=self.manifest, assets_dir=self.assets_dir
        )
        self.trainer.set_point_cloud(xyz, rgb, self.session.point_cloud_version)

    def on_keyframe_pose(self, transform_matrix: list[list[float]]) -> None:
        if self.trainer is not None:
            self.trainer.add_keyframe(transform_matrix)

    def on_image(self) -> None:
        self._frames_since_cloud += 1

    def on_point_cloud(self, xyz: np.ndarray, rgb: np.ndarray, version: int) -> None:
        self._ensure_trainer()
        if self.trainer is not None:
            self.trainer.set_point_cloud(xyz, rgb, version)
        self._frames_since_cloud = 0

    async def start(self) -> None:
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        try:
            while not self.session.is_complete:
                await asyncio.sleep(TICK_SECONDS)
                self._ensure_trainer()
                if self.trainer is None:
                    continue
                diff = await asyncio.to_thread(self.trainer.tick)
                await self.manager.broadcast(diff)
                n = len(diff.get("cells", []))
                if n:
                    await self.manager.broadcast_log(
                        f"tick {diff.get('tick')}: trained + exported {n} cell(s)")
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass


class SessionManager:
    def __init__(self, data_dir: Path, assets_dir: Path, backend_name: str | None = None):
        self.data_dir = Path(data_dir)
        self.assets_dir = Path(assets_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.backend_name = backend_name or os.environ.get("ROOMSPLAT_BACKEND", "synthetic")
        # Iterations for the session-end finishing pass (SPEC.md M6). ~7k is a good
        # quality/latency trade on the 5090; the synthetic backend ignores the count.
        self.finish_iters = int(os.environ.get("ROOMSPLAT_FINISH_ITERS", "7000"))
        self.finish_seconds = float(os.environ.get("ROOMSPLAT_FINISH_SECONDS", "60"))
        # Saved projects live OUTSIDE data_dir/assets_dir (a sibling of assets_dir) so the
        # Reset button, which wipes the live working area, never deletes a saved scene.
        self.projects_dir = Path(os.environ.get(
            "ROOMSPLAT_PROJECTS", str(self.assets_dir.parent / "projects")))
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, SessionRuntime] = {}
        self.viewers: set[Any] = set()

    # ---- viewer fan-out -----------------------------------------------------

    async def add_viewer(self, ws: Any) -> None:
        self.viewers.add(ws)
        # A late-joining browser gets the full current manifest immediately (§4), plus a
        # room-mesh notice for any session that has one — otherwise a reload restores the
        # splats (carried in the manifest) but not the LiDAR mesh (announced only when a
        # fresh frame arrives), so the mesh would vanish until the next capture frame.
        for rt in self.sessions.values():
            await ws.send_text(json.dumps(rt.manifest.snapshot()))
            if rt.session.room_mesh:
                await ws.send_text(json.dumps({
                    "type": "room_mesh",
                    "session_id": rt.session.session_id,
                    "version": rt.session.room_mesh_version,
                }))

    def remove_viewer(self, ws: Any) -> None:
        self.viewers.discard(ws)

    async def reset(self) -> None:
        """Drop all sessions and wipe their on-disk mirrors + assets, then tell viewers
        to clear. Used by the UI Reset button to start a scene from scratch."""
        for rt in list(self.sessions.values()):
            rt.session._complete = True
            await rt.stop()
        self.sessions.clear()
        for base in (self.data_dir, self.assets_dir):
            for child in base.glob("*"):
                try:
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                except OSError as e:  # pragma: no cover
                    log.warning("reset: could not remove %s: %s", child, e)
        log.info("stage=reset cleared all sessions and assets")
        await self.broadcast({"type": "reset"})
        await self.broadcast_log("reset — scene cleared, ready for a new capture", "warn")

    async def broadcast_log(self, msg: str, level: str = "info") -> None:
        """Push a concise processing line to the viewer console (bottom-right panel)."""
        await self.broadcast({"type": "log", "t": time.time(), "level": level, "msg": msg})

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        dead = []
        for ws in list(self.viewers):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.viewers.discard(ws)

    # ---- saved projects -----------------------------------------------------

    def save_project(self, session_id: str, name: str) -> dict | None:
        """Snapshot a session's splat cells + LiDAR mesh into a saved project.

        A project is a self-contained copy under projects_dir with a manifest whose asset
        URLs point at /projects-assets, so it loads identically after a Reset or a server
        restart. Returns the project metadata, or None if the session isn't in memory.
        """
        rt = self.sessions.get(session_id)
        if rt is None:
            return None
        pid = session_id
        pdir = self.projects_dir / pid
        if pdir.exists():
            shutil.rmtree(pdir)
        pdir.mkdir(parents=True)
        src = self.assets_dir / session_id
        if (src / "cells").is_dir():
            shutil.copytree(src / "cells", pdir / "cells")
        for f in src.glob("cloud.*.bin"):
            shutil.copy2(f, pdir / f.name)
        has_mesh = bool(rt.session.room_mesh)
        if has_mesh:
            (pdir / "room.bin").write_bytes(rt.session.room_mesh)
        # Rewrite asset URLs from the live /assets/<sid> prefix to the project snapshot.
        snap = rt.manifest.snapshot()
        old, new = f"/assets/{session_id}", f"/projects-assets/{pid}"
        for c in snap["cells"]:
            c["url"] = c["url"].replace(old, new)
        if snap.get("point_cloud_url"):
            snap["point_cloud_url"] = snap["point_cloud_url"].replace(old, new)
        snap["live_pose"] = None  # a saved project is static, not a live capture
        (pdir / "manifest.json").write_text(json.dumps(snap))
        meta = {"id": pid, "name": name or pid[:8], "updated": time.time(),
                "cells": len(snap["cells"]), "has_mesh": has_mesh}
        (pdir / "project.json").write_text(json.dumps(meta))
        log.info("stage=project_save id=%s name=%s cells=%d mesh=%s",
                 pid, meta["name"], meta["cells"], has_mesh)
        return meta

    def list_projects(self) -> list[dict]:
        out = []
        for pj in self.projects_dir.glob("*/project.json"):
            try:
                out.append(json.loads(pj.read_text()))
            except (OSError, ValueError):  # pragma: no cover - corrupt sidecar
                continue
        out.sort(key=lambda m: m.get("updated", 0), reverse=True)
        return out

    def load_project(self, pid: str) -> tuple[dict | None, str | None]:
        """Return (manifest_snapshot, room_mesh_url) for a saved project, or (None, None)."""
        mpath = self.projects_dir / pid / "manifest.json"
        if not mpath.is_file():
            return None, None
        snap = json.loads(mpath.read_text())
        room_url = None
        if (self.projects_dir / pid / "room.bin").is_file():
            room_url = f"/projects-assets/{pid}/room.bin"
        return snap, room_url

    # ---- ingest -------------------------------------------------------------

    async def open_session(self, msg: dict) -> SessionRuntime:
        session_id = msg["session_id"]
        # Reconnect resumes, never restarts (SPEC.md §4): if a live session with this id
        # already exists, hand it back so the client resumes from its highest ack.
        existing = self.sessions.get(session_id)
        if existing is not None and not existing.session.is_complete:
            log.info("stage=session_resume session=%s highest_ack=%d",
                     session_id, existing.session.highest_ack)
            return existing
        cam = msg["camera"]
        camera = CameraModel.from_dict(cam)
        capture = msg.get("capture", {})
        capture.setdefault("schema_version", 2)
        capture.setdefault("session_id", session_id)
        capture.setdefault("device_model", msg.get("device_model", "unknown"))
        capture.setdefault("captured_at", msg.get("captured_at", ""))
        capture.setdefault("source", "stream")
        if "exposure_locked" in msg:
            capture.setdefault("exposure_locked", msg["exposure_locked"])
        if "white_balance_locked" in msg:
            capture.setdefault("white_balance_locked", msg["white_balance_locked"])
        # The mirror starts empty; counters are recomputed as frames arrive. Never
        # carry over a stale frame_count from a copied capture (e.g. during --replay).
        capture["frame_count"] = 0
        capture["frames_dropped"] = 0
        root = self.data_dir / f"{session_id}.roomsplat"
        session = LiveSession(session_id, root, capture, camera)
        rt = SessionRuntime(self, session, self.backend_name)
        self.sessions[session_id] = rt
        await rt.start()
        log.info("stage=session_open session=%s root=%s", session_id, root)
        await self.broadcast_log(f"session {session_id[:8]} opened ({self.backend_name} backend)")
        return rt

    async def close_session(self, session_id: str) -> None:
        rt = self.sessions.get(session_id)
        if not rt:
            return
        # Idempotent: a duplicate session_complete/abort (or a disconnect after complete)
        # must not run the expensive finishing pass twice.
        if rt.session.is_complete:
            return
        rt.session.complete()
        await rt.stop()
        # Finishing pass (SPEC.md M6): train much longer than a live tick, then re-export
        # every cell at final quality. This is what turns the progressive preview into
        # the deliverable. Runs off the event loop so viewers stay responsive.
        if rt.trainer is not None:
            await self.broadcast_log(
                f"finalizing: high-quality pass (up to {self.finish_iters} iters)…", "warn")
            diff = await asyncio.to_thread(rt.trainer.finish, self.finish_iters, self.finish_seconds)
            await self.broadcast(diff)
            await self.broadcast_log(f"final scene ready: re-exported {len(diff.get('cells', []))} cell(s)")
        await self.broadcast({"type": "session_complete", "session_id": session_id})
        log.info("stage=session_complete session=%s frames=%d",
                 session_id, rt.session.stats.frame_count)
