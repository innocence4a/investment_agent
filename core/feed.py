"""価格フィード(F-1)。

- BitflyerFeed: bitFlyer Lightning 公開 WebSocket(JSON-RPC 2.0)。口座・キー不要
- SimFeed: 開発・ブラウザ検証用のランダムウォーク(実 API に依存しない)

いずれも「Ticker を順に生成する非同期イテレータ」という同一インターフェース。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import ClassVar, Protocol

import aiohttp

from core.models import SYMBOLS, Symbol, Ticker, utcnow

logger = logging.getLogger(__name__)

BITFLYER_WS_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"


class PriceFeed(Protocol):
    def stream(self) -> AsyncIterator[Ticker]: ...


class BitflyerFeed:
    """bitFlyer 公開 WebSocket から BTC_JPY / ETH_JPY のティッカーを受信する。

    切断時は指数バックオフで自動再接続する(N-2: 障害時は新規発注停止に倒すのは
    エンジン側のフィード断検知が担当)。
    """

    def __init__(self, symbols: tuple[Symbol, ...] = SYMBOLS) -> None:
        self._symbols = symbols

    async def stream(self) -> AsyncIterator[Ticker]:
        backoff = 1.0
        while True:
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.ws_connect(BITFLYER_WS_URL, heartbeat=30) as ws,
                ):
                    for sym in self._symbols:
                        await ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "subscribe",
                                "params": {"channel": f"lightning_ticker_{sym}"},
                                "id": 1,
                            }
                        )
                    backoff = 1.0
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        tick = self._parse(msg.data)
                        if tick is not None:
                            yield tick
            except (aiohttp.ClientError, TimeoutError) as e:
                logger.warning("bitFlyer WS 切断: %s。%.0f 秒後に再接続します", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _parse(self, raw: str) -> Ticker | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if data.get("method") != "channelMessage":
            return None
        params = data.get("params", {})
        channel: str = params.get("channel", "")
        message = params.get("message", {})
        sym_str = channel.removeprefix("lightning_ticker_")
        if sym_str not in SYMBOLS:
            return None
        sym: Symbol = "BTC_JPY" if sym_str == "BTC_JPY" else "ETH_JPY"
        ltp = message.get("ltp")
        if ltp is None:
            return None
        # JPY 価格は int に丸める(bitFlyer の JPY ペアは整数刻み)
        price = int(Decimal(str(ltp)).quantize(Decimal("1")))
        return Ticker(symbol=sym, price=price, ts=utcnow())


class SimFeed:
    """ランダムウォークのシミュレーションフィード(開発・検証用)。

    実データではないことはダッシュボード上の表示(モード表記)で明示する。
    """

    START_PRICE: ClassVar[dict[Symbol, int]] = {"BTC_JPY": 17_420_000, "ETH_JPY": 520_000}

    def __init__(self, interval_sec: float = 1.0, seed: int | None = None) -> None:
        self._interval = interval_sec
        self._rng = random.Random(seed)
        self._prices: dict[Symbol, float] = {s: float(p) for s, p in self.START_PRICE.items()}

    async def stream(self) -> AsyncIterator[Ticker]:
        while True:
            for sym in SYMBOLS:
                p = self._prices[sym]
                # ドリフトわずかに負〜中立のランダムウォーク(ボラ ~0.05%/tick)
                p += p * self._rng.gauss(0, 0.0005)
                self._prices[sym] = max(p, 1.0)
                yield Ticker(symbol=sym, price=int(p), ts=utcnow())
            await asyncio.sleep(self._interval)
