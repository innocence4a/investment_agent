"""記録層(F-6)のテスト: マイグレーション・往復保存・日次集計。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.models import Candle, Fill, Position, Symbol, Thought, Timeframe, utcnow
from core.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(str(tmp_path / "test.db"))
    await s.open()
    yield s
    await s.close()


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "m.db")
    for _ in range(2):  # 2 回 open してもエラーにならない(適用済みはスキップ)
        s = Store(path)
        await s.open()
        await s.close()


async def test_thought_round_trip(store: Store) -> None:
    t = await store.add_thought(
        Thought(ts=utcnow(), agent="トレーダー", kind="buy", text="テスト根拠",
                symbol="BTC_JPY", confidence=70, gate="通過")
    )
    assert t.id > 0
    loaded = await store.recent_thoughts(10)
    assert loaded[-1].text == "テスト根拠"
    assert loaded[-1].confidence == 70


async def test_fill_round_trip_and_win_loss(store: Store) -> None:
    now = utcnow()
    await store.add_fill(Fill(ts=now, symbol="BTC_JPY", side="buy",
                              qty=Decimal("0.001"), price=10_000_000, notional=10_000, fee=15))
    await store.add_fill(Fill(ts=now, symbol="BTC_JPY", side="sell", qty=Decimal("0.001"),
                              price=10_100_000, notional=10_100, fee=15, realized_pnl=70))
    await store.add_fill(Fill(ts=now, symbol="ETH_JPY", side="sell", qty=Decimal("0.01"),
                              price=500_000, notional=5_000, fee=8, realized_pnl=-30))
    fills = await store.recent_fills(10)
    assert len(fills) == 3
    assert fills[0].qty == Decimal("0.001")
    wins, losses = await store.win_loss_counts()
    assert (wins, losses) == (1, 1)
    assert await store.trade_count() == 3


async def test_fills_since_filters_by_ts(store: Store) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    await store.add_fill(Fill(ts=old, symbol="BTC_JPY", side="buy",
                              qty=Decimal("1"), price=1, notional=1, fee=0))
    await store.add_fill(Fill(ts=utcnow(), symbol="BTC_JPY", side="buy",
                              qty=Decimal("1"), price=1, notional=1, fee=0))
    cutoff = (utcnow() - timedelta(hours=1)).isoformat()
    assert len(await store.fills_since(cutoff)) == 1


async def test_candle_upsert_and_load(store: Store) -> None:
    c = Candle(symbol="BTC_JPY", tf=Timeframe.M5,
               open_ts=datetime(2026, 7, 12, 3, 0, tzinfo=UTC), o=1, h=2, l=1, c=2)
    await store.upsert_candle(c)
    c.c = 5
    c.h = 5
    await store.upsert_candle(c)  # 同じ足の更新は上書き
    loaded = await store.load_candles("BTC_JPY", Timeframe.M5)
    assert len(loaded) == 1
    assert loaded[0].c == 5


async def test_broker_state_round_trip(store: Store) -> None:
    await store.save_broker(
        123_456,
        {"BTC_JPY": Position(symbol="BTC_JPY", qty=Decimal("0.5"),
                             avg_cost=Decimal("10000000.25"))},
    )
    cash, positions = await store.load_broker()
    assert cash == 123_456
    assert positions[0].qty == Decimal("0.5")
    assert positions[0].avg_cost == Decimal("10000000.25")
    # 空で上書きすればポジションは消える
    await store.save_broker(999, {})
    cash, positions = await store.load_broker()
    assert (cash, positions) == (999, [])


async def test_incr_state_float_accumulates_atomically(store: Store) -> None:
    import asyncio

    # 並行加算しても取りこぼさない(LLM 月次コストカウンタの read-modify-write 対策)
    await asyncio.gather(*(store.incr_state_float("cost", 0.5) for _ in range(20)))
    assert abs(float(await store.get_state("cost")) - 10.0) < 1e-6


async def test_save_broker_is_atomic_under_concurrent_writes(store: Store) -> None:
    import asyncio

    # save_broker(DELETE→INSERT)と他の書き込みが並行しても部分コミットにならない
    pos: dict[Symbol, Position] = {
        "BTC_JPY": Position(symbol="BTC_JPY", qty=Decimal("0.1"), avg_cost=Decimal(1_000_000))
    }

    async def churn() -> None:
        for i in range(20):
            await store.add_equity_snapshot(
                datetime(2026, 7, 12, 0, 0, i, tzinfo=UTC), 1_000_000, 900_000
            )

    async def save_repeatedly() -> None:
        for _ in range(20):
            await store.save_broker(500_000, pos)

    await asyncio.gather(churn(), save_repeatedly())
    cash, positions = await store.load_broker()
    assert cash == 500_000
    assert len(positions) == 1  # ポジション記録が消えていない


async def test_app_state(store: Store) -> None:
    assert await store.get_state("halted", "0") == "0"
    await store.set_state("halted", "1")
    assert await store.get_state("halted") == "1"


async def test_equity_snapshots(store: Store) -> None:
    now = utcnow()
    await store.add_equity_snapshot(now, 1_000_000, 900_000)
    await store.add_equity_snapshot(now, 1_000_100, 900_000)  # 同時刻は上書き
    points = await store.recent_equity(10)
    assert len(points) == 1
    assert points[0].equity == 1_000_100
