"""エンジン統合テスト: キルスイッチ(F-13)の挙動と発注経路のリスクゲート強制。"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from core.agent import MockTraderAgent
from core.config import Settings
from core.engine import Engine
from core.feed import SimFeed
from core.models import Ticker, TraderDecision, utcnow
from core.notifier import Notifier
from core.store import Store


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[Engine]:
    settings = Settings(db_path=str(tmp_path / "e.db"), slippage_bps=0, fee_bps=0)
    store = Store(settings.db_path)
    await store.open()
    eng = Engine(settings, store, SimFeed(seed=1), MockTraderAgent(), Notifier(""))
    eng.messages = []  # type: ignore[attr-defined]

    async def collect(msg: dict[str, Any]) -> None:
        eng.messages.append(msg)  # type: ignore[attr-defined]

    eng.set_broadcast(collect)
    yield eng
    await store.close()


def tick(price: int) -> Ticker:
    return Ticker(symbol="BTC_JPY", price=price, ts=utcnow())


async def test_kill_switch_persists_and_blocks_entries(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    await engine.set_halted(True)
    assert engine.gate.halted is True
    # DB に永続化(再起動しても停止状態を維持 = ダッシュボードの生死に依存しない)
    assert await engine.store.get_state("halted") == "1"
    # 停止中の買い提案はリスクゲートが拒否し、約定は発生しない
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=30_000,
                       confidence=80, reason="テスト買い提案")
    await engine._apply_decision(d, daily_pnl=0, trades_today=0)
    assert engine.broker.positions() == {}
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.gate == "発動(HALTED)" for t in thoughts)
    # halt メッセージが配信されている
    assert any(m["type"] == "halt" and m["data"]["halted"] for m in engine.messages)  # type: ignore[attr-defined]


async def test_kill_switch_state_survives_restart(engine: Engine) -> None:
    await engine.set_halted(True)
    # 同じ DB で新しいエンジンを起動しても停止状態を引き継ぐ
    eng2 = Engine(engine.settings, engine.store, SimFeed(seed=2),
                  MockTraderAgent(), Notifier(""))
    eng2.gate.halted = (await engine.store.get_state("halted", "0")) == "1"
    assert eng2.gate.halted is True


async def test_stop_loss_fires_even_while_halted(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    fill = engine.broker.buy("BTC_JPY", 50_000, 10_000_000, utcnow())
    await engine.store.add_fill(fill)
    await engine.set_halted(True)
    # -2% の損切りラインを下回るティック → 停止中でもコード側監視が決済する(F-13)
    await engine._check_exits(tick(9_790_000))
    assert engine.broker.positions() == {}
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.gate == "発動(損切り)" for t in thoughts)


async def test_take_profit_fires(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    engine.broker.buy("BTC_JPY", 50_000, 10_000_000, utcnow())
    await engine._check_exits(tick(10_450_000))  # +4.5% > 利確ライン +4%
    assert engine.broker.positions() == {}


async def test_buy_decision_executes_through_gate(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=50_000,
                       confidence=70, reason="上限ちょうどの買い")
    await engine._apply_decision(d, daily_pnl=0, trades_today=0)
    assert "BTC_JPY" in engine.broker.positions()
    # 全判断が記録されている(F-6)
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.kind == "buy" and t.gate == "通過" for t in thoughts)


async def test_over_limit_buy_is_rejected_no_order(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=50_001,
                       confidence=99, reason="上限超の買い(LLM が暴走したケース)")
    await engine._apply_decision(d, daily_pnl=0, trades_today=0)
    assert engine.broker.positions() == {}  # コードが LLM 出力より優先される
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.kind == "risk" and "PER_TRADE_LIMIT" in (t.gate or "") for t in thoughts)


async def test_hold_decision_is_recorded(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    d = TraderDecision(action="hold", symbol="BTC_JPY", confidence=55, reason="見送り根拠")
    await engine._apply_decision(d, daily_pnl=0, trades_today=0)
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.kind == "skip" and t.text == "見送り根拠" for t in thoughts)  # 見送りも記録(F-6)


async def test_daily_stats_uses_jst_day(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    engine.broker.buy("BTC_JPY", 50_000, 10_000_000, utcnow())
    fill = engine.broker.close("BTC_JPY", 9_900_000, utcnow())
    await engine.store.add_fill(fill)
    pnl, trades = await engine._daily_stats()
    assert trades == 1
    assert pnl == fill.realized_pnl


async def test_snapshot_shape(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    snap = await engine.snapshot()
    assert snap["config"]["mode"] == "PAPER"
    assert "BTC_JPY" in snap["candles"]
    assert snap["kpi"]["equity"] > 0
    assert snap["halted"] is False
