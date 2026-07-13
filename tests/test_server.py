"""Hub.broadcast の回帰テスト: 接続/切断との競合・送信失敗クライアントの切り離し。"""

from typing import Any, cast

from aiohttp import web

from core.server import Hub


class FakeWS:
    """web.WebSocketResponse の送信部分だけを模したテスト用ダブル。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail = False
        self.on_send: Any = None  # 送信中に呼ぶフック(競合の再現用)

    async def send_str(self, raw: str) -> None:
        if self.on_send is not None:
            self.on_send()
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(raw)

    async def close(self, *, code: int = 1000, message: bytes = b"") -> bool:
        return True


def as_ws(fake: FakeWS) -> web.WebSocketResponse:
    return cast(web.WebSocketResponse, fake)


async def test_broadcast_survives_concurrent_disconnect() -> None:
    """送信中の add/remove(ブラウザのリロード等)でイテレーションが壊れない。"""
    hub = Hub()
    a, b, c = FakeWS(), FakeWS(), FakeWS()
    for ws in (a, b, c):
        hub.add(as_ws(ws))
    joiner = FakeWS()

    def churn() -> None:
        # a の送信中に b が切断し、新規クライアントが接続してくる(集合が変化する)
        hub.remove(as_ws(b))
        hub.add(as_ws(joiner))

    a.on_send = churn
    await hub.broadcast({"type": "tick", "data": {}})  # RuntimeError にならないこと
    assert a.sent  # a には届いている


async def test_broadcast_drops_failing_client_and_continues() -> None:
    """1 クライアントの送信失敗(ConnectionError 以外も)で他への配信が止まらない。"""
    hub = Hub()
    bad, good = FakeWS(), FakeWS()
    bad.fail = True
    hub.add(as_ws(bad))
    hub.add(as_ws(good))
    await hub.broadcast({"type": "kpi", "data": {}})
    assert good.sent  # 正常クライアントには届く
    # 失敗クライアントは切り離され、以後の配信対象から外れる
    await hub.broadcast({"type": "kpi", "data": {}})
    assert len(good.sent) == 2
    assert bad.sent == []
