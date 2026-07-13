"""ローソク足構築(F-1)のテスト: タイムフレーム境界の整列。"""

from datetime import UTC, datetime

from core.market import MarketState, align_open
from core.models import Ticker, Timeframe


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_align_5m() -> None:
    assert align_open(ts("2026-07-12 03:07:59"), Timeframe.M5) == ts("2026-07-12 03:05:00")
    assert align_open(ts("2026-07-12 03:05:00"), Timeframe.M5) == ts("2026-07-12 03:05:00")
    assert align_open(ts("2026-07-12 03:04:59"), Timeframe.M5) == ts("2026-07-12 03:00:00")


def test_align_4h_and_1d() -> None:
    assert align_open(ts("2026-07-12 07:59:59"), Timeframe.H4) == ts("2026-07-12 04:00:00")
    assert align_open(ts("2026-07-12 23:59:59"), Timeframe.D1) == ts("2026-07-12 00:00:00")


def test_align_week_starts_monday() -> None:
    # 2026-07-12 は日曜 → 週足の開始は 7/6(月)
    assert align_open(ts("2026-07-12 12:00:00"), Timeframe.W1) == ts("2026-07-06 00:00:00")


def test_align_month_calendar() -> None:
    assert align_open(ts("2026-07-31 23:59:59"), Timeframe.MO1) == ts("2026-07-01 00:00:00")
    assert align_open(ts("2026-08-01 00:00:00"), Timeframe.MO1) == ts("2026-08-01 00:00:00")


def test_tick_updates_existing_candle_and_opens_new() -> None:
    m = MarketState()
    m.update(Ticker(symbol="BTC_JPY", price=100, ts=ts("2026-07-12 03:00:01")))
    m.update(Ticker(symbol="BTC_JPY", price=110, ts=ts("2026-07-12 03:01:00")))
    m.update(Ticker(symbol="BTC_JPY", price=90, ts=ts("2026-07-12 03:04:59")))
    c5 = m.candles["BTC_JPY"][Timeframe.M5]
    assert len(c5) == 1
    assert (c5[0].o, c5[0].h, c5[0].l, c5[0].c) == (100, 110, 90, 90)
    # 境界を跨ぐと新しい足が開く
    m.update(Ticker(symbol="BTC_JPY", price=95, ts=ts("2026-07-12 03:05:00")))
    c5 = m.candles["BTC_JPY"][Timeframe.M5]
    assert len(c5) == 2
    assert (c5[1].o, c5[1].c) == (95, 95)
    # 1時間足は同じ足のまま
    assert len(m.candles["BTC_JPY"][Timeframe.H1]) == 1
    assert m.last_price["BTC_JPY"] == 95
