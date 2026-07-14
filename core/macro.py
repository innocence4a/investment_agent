"""関連指標(F-18): 恐怖指数・Fear & Greed・ゴールド・S&P500 先物・DXY・米10年金利。

- 無料ソース(15〜20 分遅延で可): Fear & Greed = alternative.me、それ以外は
  Yahoo Finance の公開チャート API(キー不要)
- ダッシュボード常時表示に加え、LLM 判断のインプットにも渡す
- 表示・判断材料であり金銭計算ではないため float を許容
- SimMacroSource は開発・検証用(実 API 非依存)
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, ClassVar, Protocol

import aiohttp

from core.models import MACRO_KEYS, MacroKey, MacroSnapshot, MacroTile, utcnow

logger = logging.getLogger(__name__)

FNG_URL = "https://api.alternative.me/fng/?limit=1"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo のシンボル対応(^TNX は利回り×10 で返るため scale で補正)
YAHOO_SYMBOLS: dict[MacroKey, tuple[str, float]] = {
    "vix": ("^VIX", 1.0),
    "gold_usd": ("GC=F", 1.0),
    "sp500": ("ES=F", 1.0),
    "dxy": ("DX-Y.NYB", 1.0),
    "us10y": ("^TNX", 0.1),
}
USER_AGENT = "Mozilla/5.0 (investment-agent; paper-trading dashboard)"


def parse_fng(data: dict[str, Any]) -> float:
    """alternative.me Fear & Greed API の応答から値(0-100)を取り出す。"""
    return float(data["data"][0]["value"])


def parse_yahoo_chart(data: dict[str, Any]) -> tuple[float, float | None]:
    """Yahoo chart API の応答から (現在値, 前日終値) を取り出す。"""
    meta = data["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    prev_raw = meta.get("chartPreviousClose", meta.get("previousClose"))
    prev = float(prev_raw) if prev_raw is not None else None
    return price, prev


def change_pct(value: float, base: float | None) -> float | None:
    if base is None or base == 0:
        return None
    return (value - base) / base * 100


class MacroSource(Protocol):
    async def fetch(self) -> MacroSnapshot: ...


class RealMacroSource:
    """無料ソースから取得する本実装。個別の失敗はその指標を欠損にするだけで全体は続行。"""

    async def fetch(self) -> MacroSnapshot:
        snap = MacroSnapshot(fetched_at=utcnow())
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": USER_AGENT}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            results = await asyncio.gather(
                self._fetch_fng(session),
                *(self._fetch_yahoo(session, key) for key in YAHOO_SYMBOLS),
                return_exceptions=True,
            )
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("関連指標の取得に失敗: %s", r)
            elif r is not None:
                snap.tiles[r.key] = r
        return snap

    async def _fetch_fng(self, session: aiohttp.ClientSession) -> MacroTile | None:
        async with session.get(FNG_URL) as res:
            res.raise_for_status()
            value = parse_fng(await res.json())
        return MacroTile(key="fear_greed", value=value, ts=utcnow())

    async def _fetch_yahoo(
        self, session: aiohttp.ClientSession, key: MacroKey
    ) -> MacroTile | None:
        symbol, scale = YAHOO_SYMBOLS[key]
        url = YAHOO_CHART_URL.format(symbol=symbol)
        async with session.get(url, params={"range": "1d", "interval": "15m"}) as res:
            res.raise_for_status()
            price, prev = parse_yahoo_chart(await res.json())
        return MacroTile(
            key=key,
            value=price * scale,
            change_pct=change_pct(price, prev),
            ts=utcnow(),
        )


class SimMacroSource:
    """開発・検証用のランダムウォーク(モックの初期値・変動幅に準拠)。"""

    BASE: ClassVar[dict[MacroKey, tuple[float, float]]] = {
        "fear_greed": (62.0, 1.5),
        "vix": (18.4, 0.25),
        "gold_usd": (3324.0, 5.0),
        "sp500": (6128.0, 6.0),
        "dxy": (103.4, 0.08),
        "us10y": (4.23, 0.015),
    }

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._values = {k: v for k, (v, _) in self.BASE.items()}
        self._start = dict(self._values)

    async def fetch(self) -> MacroSnapshot:
        snap = MacroSnapshot(fetched_at=utcnow())
        for key in MACRO_KEYS:
            base, step = self.BASE[key]
            v = self._values[key] + (self._rng.random() - 0.5) * step * 2
            v = max(v, base * 0.5)
            if key == "fear_greed":
                v = min(max(v, 0.0), 100.0)
            self._values[key] = v
            snap.tiles[key] = MacroTile(
                key=key, value=round(v, 2),
                change_pct=change_pct(v, self._start[key]), ts=utcnow(),
            )
        return snap
