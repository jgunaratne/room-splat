#!/usr/bin/env bash
cd /home/junius/git/room-splat/server
export ROOMSPLAT_WEB=/home/junius/git/room-splat/web/dist
export ROOMSPLAT_DATA=/home/junius/git/room-splat/server/data/captures
export ROOMSPLAT_ASSETS=/home/junius/git/room-splat/server/data/assets
export ROOMSPLAT_BACKEND=gsplat
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
