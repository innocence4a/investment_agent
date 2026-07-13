"""マーケット状態の集約: ティックからローソク足を構築し、直近履歴を保持する(F-1)。

ローソク足の境界はすべて UTC(日足=UTC 0時、週足=月曜、月足=暦月)。
表示上の時刻変換はダッシュボード側で JST に行う。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.models import SYMBOLS, TF_MINUTES, Candle, Symbol, Ticker, Timeframe

MAX_CANDLES = 500  # タイムフレームごとにメモリ保持する本数


def align_open(ts: datetime, tf: Timeframe) -> datetime:
    """ts が属するローソク足の開始時刻(UTC)を返す。"""
    ts = ts.astimezone(UTC)
    if tf is Timeframe.MO1:
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if tf is Timeframe.W1:
        d = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    mins = TF_MINUTES[tf]
    epoch_min = int(ts.timestamp()) // 60
    aligned = (epoch_min // mins) * mins
    return datetime.fromtimestamp(aligned * 60, tz=UTC)


class MarketState:
    """銘柄ごとの最新価格と全タイムフレームのローソク足を保持する。"""

    def __init__(self) -> None:
        self.last_price: dict[Symbol, int] = {}
        self.last_ts: dict[Symbol, datetime] = {}
        self.candles: dict[Symbol, dict[Timeframe, list[Candle]]] = {
            s: {tf: [] for tf in Timeframe} for s in SYMBOLS
        }

    def prime(self, candles: list[Candle]) -> None:
        """DB から読み込んだ過去足で初期化する(open_ts 昇順であること)。"""
        for c in candles:
            arr = self.candles[c.symbol][c.tf]
            arr.append(c)
            del arr[:-MAX_CANDLES]

    def update(self, tick: Ticker) -> list[Candle]:
        """ティックを反映し、更新された(または新規の)足を返す。"""
        self.last_price[tick.symbol] = tick.price
        self.last_ts[tick.symbol] = tick.ts
        updated: list[Candle] = []
        for tf in Timeframe:
            arr = self.candles[tick.symbol][tf]
            open_ts = align_open(tick.ts, tf)
            if arr and arr[-1].open_ts == open_ts:
                cur = arr[-1]
                cur.c = tick.price
                cur.h = max(cur.h, tick.price)
                cur.l = min(cur.l, tick.price)
                updated.append(cur)
            else:
                cur = Candle(
                    symbol=tick.symbol, tf=tf, open_ts=open_ts,
                    o=tick.price, h=tick.price, l=tick.price, c=tick.price,
                )
                arr.append(cur)
                del arr[:-MAX_CANDLES]
                updated.append(cur)
        return updated

    def closes(self, symbol: Symbol, tf: Timeframe, n: int = 120) -> list[float]:
        """指標計算用の終値列(float)。金銭計算には使わない。"""
        return [float(c.c) for c in self.candles[symbol][tf][-n:]]
