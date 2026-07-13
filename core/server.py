"""コア → ダッシュボード配信サーバー。

- WS /ws        : 接続時にスナップショット、以後は差分 push(N-1: 1 秒以内)
- POST /api/halt: 緊急停止/再開(F-13 の入口。状態はコア側が保持)
- GET  /api/health
- /             : dashboard/dist があれば静的配信(本番想定。開発時は Vite dev server)

認証(N-4): IA_AUTH_TOKEN 設定時は WS の ?token= / API の Authorization: Bearer を検査。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from core.engine import Engine

logger = logging.getLogger(__name__)

DIST_DIR = Path(__file__).parent.parent / "dashboard" / "dist"


class Hub:
    """接続中の WebSocket クライアント集合と broadcast。

    broadcast はエンジンのフィード消費・損切り監視と同じタスク文脈で呼ばれるため、
    ここで例外を漏らしたり長時間ブロックしたりしてはならない:
    - クライアント集合は接続/切断タスクと競合するためスナップショットを反復する
    - 送信失敗は種類を問わず当該クライアントの切断として扱う
    - 遅いクライアントはタイムアウトで切り捨てる(1 クライアントが全体を塞がない)
    """

    SEND_TIMEOUT_SEC = 5.0

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()

    async def broadcast(self, msg: dict[str, Any]) -> None:
        if not self._clients:
            return
        raw = json.dumps(msg, ensure_ascii=False)
        for ws in list(self._clients):  # イテレーション中の add/remove と競合しないよう複製
            try:
                async with asyncio.timeout(self.SEND_TIMEOUT_SEC):
                    await ws.send_str(raw)
            except Exception:
                self._clients.discard(ws)
                with contextlib.suppress(Exception):
                    await ws.close(code=1011, message=b"send failed")

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def remove(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return True
    return request.query.get("token", "") == token


def build_app(engine: Engine, auth_token: str = "") -> web.Application:
    hub = Hub()
    engine.set_broadcast(hub.broadcast)
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "halted": engine.gate.halted})

    async def halt(request: web.Request) -> web.Response:
        if not _authorized(request, auth_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            halted = bool(body["halted"])
        except (json.JSONDecodeError, KeyError):
            return web.json_response({"error": "bad request"}, status=400)
        await engine.set_halted(halted)
        return web.json_response({"halted": engine.gate.halted})

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        if not _authorized(request, auth_token):
            await ws.prepare(request)
            await ws.close(code=4401, message=b"unauthorized")
            return ws
        await ws.prepare(request)
        hub.add(ws)
        try:
            snapshot = await engine.snapshot()
            await ws.send_str(
                json.dumps({"type": "snapshot", "data": snapshot}, ensure_ascii=False)
            )
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            hub.remove(ws)
        return ws

    app.router.add_get("/api/health", health)
    app.router.add_post("/api/halt", halt)
    app.router.add_get("/ws", ws_handler)

    if DIST_DIR.exists():
        async def index(_: web.Request) -> web.FileResponse:
            return web.FileResponse(DIST_DIR / "index.html")

        app.router.add_get("/", index)
        app.router.add_static("/assets", DIST_DIR / "assets")
    return app
