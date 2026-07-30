"""エンジン統合テスト: キルスイッチ(F-13)の挙動と発注経路のリスクゲート強制。"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.advisor import MockAdvisor
from core.agent import MockTraderAgent
from core.calendar import EconomicCalendar
from core.config import Settings
from core.engine import Engine
from core.feed import SimFeed
from core.models import (
    EconomicEvent,
    Fill,
    MacroSnapshot,
    MacroTile,
    RestraintMode,
    RestraintState,
    Ticker,
    TraderDecision,
    utcnow,
)
from core.notifier import Notifier
from core.store import Store


class CaptureNotifier(Notifier):
    """送信内容を記録するテスト用 Notifier。"""

    def __init__(self) -> None:
        super().__init__("")
        self.sent: list[tuple[str, str]] = []

    async def send(self, text: str, *, level: str = "info") -> None:
        self.sent.append((level, text))


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[Engine]:
    settings = Settings(db_path=str(tmp_path / "e.db"), slippage_bps=0, fee_bps=0)
    store = Store(settings.db_path)
    await store.open()
    eng = Engine(settings, store, SimFeed(seed=1), MockTraderAgent(), CaptureNotifier())
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
    await engine._apply_decision(d)
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
    await engine._apply_decision(d)
    assert "BTC_JPY" in engine.broker.positions()
    # 全判断が記録されている(F-6)
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.kind == "buy" and t.gate == "通過" for t in thoughts)


async def test_over_limit_buy_is_rejected_no_order(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=50_001,
                       confidence=99, reason="上限超の買い(LLM が暴走したケース)")
    await engine._apply_decision(d)
    assert engine.broker.positions() == {}  # コードが LLM 出力より優先される
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.kind == "risk" and "PER_TRADE_LIMIT" in (t.gate or "") for t in thoughts)


async def test_hold_decision_is_recorded(engine: Engine) -> None:
    engine.market.update(tick(10_000_000))
    d = TraderDecision(action="hold", symbol="BTC_JPY", confidence=55, reason="見送り根拠")
    await engine._apply_decision(d)
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


# ── レビュー指摘の回帰テスト ─────────────────────────


async def test_gate_uses_fresh_daily_stats_at_order_time(engine: Engine) -> None:
    """LLM 応答待ちの間に日次損失が上限到達しても、発注直前の再取得で拒否される。"""
    engine.market.update(tick(10_000_000))
    # LLM 呼び出し「後」を模擬: 損切り約定で日次損失が上限ちょうどに到達済み
    await engine.store.add_fill(
        Fill(ts=utcnow(), symbol="BTC_JPY", side="sell", qty=Decimal("0.005"),
             price=10_000_000, notional=50_000, fee=0,
             realized_pnl=-engine.settings.risk.max_daily_loss_jpy)
    )
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=30_000,
                       confidence=80, reason="古いスナップショットに基づく買い提案")
    await engine._apply_decision(d)
    assert engine.broker.positions() == {}
    thoughts = await engine.store.recent_thoughts(10)
    assert any("DAILY_LOSS" in (t.gate or "") for t in thoughts)


async def test_stale_symbol_order_is_rejected(engine: Engine) -> None:
    """フィード途絶中の銘柄は古い価格での発注を拒否する(銘柄別判定)。"""
    old = Ticker(
        symbol="BTC_JPY", price=10_000_000,
        ts=utcnow() - timedelta(seconds=engine.settings.feed_stale_sec + 5),
    )
    engine.market.update(old)
    assert "BTC_JPY" in engine._stale_symbols()
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=30_000,
                       confidence=80, reason="途絶中の買い提案")
    await engine._apply_decision(d)
    assert engine.broker.positions() == {}
    thoughts = await engine.store.recent_thoughts(10)
    assert any("FEED_STALE" in (t.gate or "") for t in thoughts)
    # 新しいティックが来れば stale 解除
    engine.market.update(tick(10_000_000))
    assert "BTC_JPY" not in engine._stale_symbols()


async def test_supervised_task_notifies_and_restarts(engine: Engine) -> None:
    """バックグラウンドタスクの例外は通知され、タスクは再起動される(無言停止しない)。"""
    calls = 0
    resumed = asyncio.Event()

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        resumed.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(engine._supervised("テストタスク", flaky))
    try:
        await asyncio.wait_for(resumed.wait(), timeout=10)  # 初回バックオフ(2 秒)後に再起動
    finally:
        task.cancel()
    assert calls == 2
    notifier = engine.notifier
    assert isinstance(notifier, CaptureNotifier)
    assert any("テストタスク" in text and level == "error" for level, text in notifier.sent)


class FixedCalendar(EconomicCalendar):
    """テスト用: イベントを固定注入するカレンダー(ルール生成なし)。"""

    def __init__(self, events: list[EconomicEvent]) -> None:
        self._events = sorted(events, key=lambda e: e.ts)
        self.static_missing = False

    def refresh(self, *, now: datetime | None = None) -> None:
        pass  # 固定イベントを維持


def cpi_event(ts: "datetime", importance: str = "hi") -> EconomicEvent:
    return EconomicEvent(
        id="cpi-test", label="米CPI(テスト)",
        importance=importance,  # type: ignore[arg-type]
        ts=ts, window_before_min=30, window_after_min=30,
    )


async def test_restraint_no_entry_blocks_buy_at_gate(engine: Engine) -> None:
    """リスク管理の抑制状態はゲート層で強制され、トレーダー提案より優先される。"""
    engine.market.update(tick(10_000_000))
    engine.gate.restraint = RestraintState(
        mode=RestraintMode.NO_ENTRY, reasons=["米CPI の発表前後の取引抑制ウィンドウ内"]
    )
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=10_000,
                       confidence=90, reason="抑制中でも強気の買い提案(LLM 暴走ケース)")
    await engine._apply_decision(d)
    assert engine.broker.positions() == {}
    thoughts = await engine.store.recent_thoughts(10)
    assert any("RESTRAINT_NO_ENTRY" in (t.gate or "") for t in thoughts)


async def test_risk_agent_activates_and_releases_on_calendar(engine: Engine) -> None:
    """カレンダーの抑制ウィンドウで自動発動し、ウィンドウ終了で自動解除される。"""
    engine.market.update(tick(10_000_000))
    engine.calendar = FixedCalendar([cpi_event(utcnow())])  # いまがウィンドウ内
    await engine._evaluate_restraint()
    assert engine.gate.restraint.mode is RestraintMode.NO_ENTRY
    thoughts = await engine.store.recent_thoughts(10)
    assert any(t.agent == "リスク管理" and "米CPI" in t.text for t in thoughts)
    assert any(m["type"] == "restraint" for m in engine.messages)  # type: ignore[attr-defined]
    # ウィンドウ外になれば解除
    engine.calendar = FixedCalendar([cpi_event(utcnow() + timedelta(hours=6))])
    await engine._evaluate_restraint()
    released: RestraintMode = engine.gate.restraint.mode
    assert released is RestraintMode.NONE
    thoughts = await engine.store.recent_thoughts(10)
    assert any("解除" in t.text for t in thoughts)


async def test_risk_agent_no_spam_when_state_unchanged(engine: Engine) -> None:
    engine.calendar = FixedCalendar([cpi_event(utcnow())])
    await engine._evaluate_restraint()
    n_before = len(await engine.store.recent_thoughts(50))
    await engine._evaluate_restraint()  # 同じ状態 → 追加の記録なし
    assert len(await engine.store.recent_thoughts(50)) == n_before


async def test_consecutive_losses_counts_trailing_sells(engine: Engine) -> None:
    def sell(pnl: int) -> Fill:
        return Fill(ts=utcnow(), symbol="BTC_JPY", side="sell", qty=Decimal("0.001"),
                    price=10_000_000, notional=10_000, fee=0, realized_pnl=pnl)

    def buy() -> Fill:
        return Fill(ts=utcnow(), symbol="BTC_JPY", side="buy", qty=Decimal("0.001"),
                    price=10_000_000, notional=10_000, fee=0)

    for f in (sell(100), sell(-1), buy(), sell(-1), sell(-1)):
        await engine.store.add_fill(f)
    # 買いは連敗判定の対象外。勝ちが出るまで遡って 3 連敗
    assert await engine._consecutive_losses_today() == 3


async def test_advisor_records_advice_thought(engine: Engine) -> None:
    engine.advisor = MockAdvisor()
    engine.market.update(tick(10_000_000))
    await engine._run_advisor()
    thoughts = await engine.store.recent_thoughts(10)
    advice = [t for t in thoughts if t.kind == "advice"]
    assert len(advice) == 1
    assert advice[0].agent == "相談役"
    assert advice[0].text  # 日本語の所感が入っている
    notifier = engine.notifier
    assert isinstance(notifier, CaptureNotifier)
    assert any("相談役" in text for _, text in notifier.sent)
    # 相談役はポジション・現金に一切影響しない(発注経路に接続しない)
    assert engine.broker.cash == engine.settings.start_capital_jpy
    assert engine.broker.positions() == {}


async def test_trader_context_includes_macro_calendar_restraint(engine: Engine) -> None:
    engine.macro = MacroSnapshot(
        tiles={"vix": MacroTile(key="vix", value=27.5, change_pct=3.0, ts=utcnow())},
        fetched_at=utcnow(),
    )
    engine.calendar = FixedCalendar([cpi_event(utcnow() + timedelta(hours=2))])
    engine.gate.restraint = RestraintState(mode=RestraintMode.SIZE_HALF, reasons=["VIX 上昇"])
    extra = engine._trader_extra_context()
    assert extra["macro_indicators"]["vix"]["value"] == 27.5
    assert extra["upcoming_events"][0]["label"] == "米CPI(テスト)"
    assert 110 <= extra["upcoming_events"][0]["minutes_until"] <= 120
    assert extra["restraint"]["mode"] == "size_half"


async def test_stale_macro_excluded_from_context(engine: Engine) -> None:
    old = utcnow() - timedelta(seconds=engine.settings.macro_stale_sec + 60)
    engine.macro = MacroSnapshot(
        tiles={"vix": MacroTile(key="vix", value=50.0, change_pct=None, ts=old)},
        fetched_at=old,
    )
    # 鮮度切れの VIX は判断材料からも(誤発動を防ぐため)リスク評価からも外れる
    assert engine._fresh_vix() is None
    assert "macro_indicators" not in engine._trader_extra_context()


async def test_restraint_reevaluated_at_order_time(engine: Engine) -> None:
    """LLM 応答待ち中にウィンドウ入りしても、発注直前の再評価でゲートが拒否する。"""
    engine.market.update(tick(10_000_000))
    engine.calendar = FixedCalendar([cpi_event(utcnow())])  # 既にウィンドウ内
    assert engine.gate.restraint.mode is RestraintMode.NONE  # 定期評価はまだ来ていない想定
    d = TraderDecision(action="buy", symbol="BTC_JPY", notional_jpy=10_000,
                       confidence=80, reason="ウィンドウ入り前の古いスナップショットでの提案")
    await engine._apply_decision(d)
    assert engine.broker.positions() == {}
    after: RestraintMode = engine.gate.restraint.mode
    assert after is RestraintMode.NO_ENTRY
    thoughts = await engine.store.recent_thoughts(10)
    assert any("RESTRAINT_NO_ENTRY" in (t.gate or "") for t in thoughts)


async def test_macro_partial_failure_keeps_fresh_old_values(engine: Engine) -> None:
    """VIX だけ取得失敗しても、鮮度内の旧 VIX を保持する(フェイルオープン防止)。"""
    engine.macro = MacroSnapshot(
        tiles={"vix": MacroTile(key="vix", value=30.0, change_pct=None, ts=utcnow())},
        fetched_at=utcnow(),
    )

    class PartialSource:
        async def fetch(self) -> MacroSnapshot:
            return MacroSnapshot(
                tiles={"gold_usd": MacroTile(key="gold_usd", value=3300.0,
                                             change_pct=None, ts=utcnow())},
                fetched_at=utcnow(),
            )

    engine.macro_source = PartialSource()
    await engine._macro_once()
    assert engine._fresh_vix() == 30.0  # 旧 VIX が残る → VIX 由来の抑制は解除されない
    assert "gold_usd" in engine.macro.tiles


async def test_macro_fetch_exception_does_not_propagate(engine: Engine) -> None:
    """取得の失敗は握りつぶして継続(監督機構の連続障害→緊急停止に連鎖させない)。"""

    class BrokenSource:
        async def fetch(self) -> MacroSnapshot:
            raise RuntimeError("network down")

    engine.macro_source = BrokenSource()
    await engine._macro_once()  # 例外が伝播しないこと
    assert engine.macro.tiles == {}


async def test_engine_start_restores_state_and_evaluates_restraint(tmp_path: Path) -> None:
    """Engine.start() の実経路: 緊急停止・ブローカー復元、起動時の抑制評価、ループ登録。"""
    from core.models import Position

    settings = Settings(db_path=str(tmp_path / "s.db"), slippage_bps=0, fee_bps=0)
    store = Store(settings.db_path)
    await store.open()
    await store.set_state("halted", "1")
    await store.save_broker(
        777_000,
        {"BTC_JPY": Position(symbol="BTC_JPY", qty=Decimal("0.01"),
                             avg_cost=Decimal(10_000_000))},
    )
    from core.advisor import MockAdvisor as _MockAdvisor
    from core.macro import SimMacroSource

    eng = Engine(
        settings, store, SimFeed(seed=3), MockTraderAgent(), CaptureNotifier(),
        macro_source=SimMacroSource(seed=3),
        calendar=FixedCalendar([cpi_event(utcnow())]),  # 起動時点でウィンドウ内
        advisor=_MockAdvisor(),
    )
    await eng.start()
    try:
        assert eng.gate.halted is True  # 緊急停止状態を復元
        assert eng.broker.cash == 777_000  # ブローカー状態を復元
        assert "BTC_JPY" in eng.broker.positions()
        # 再起動直後の抑制空白を作らない: ループ開始を待たず起動時に評価済み
        assert eng.gate.restraint.mode is RestraintMode.NO_ENTRY
        assert len(eng._tasks) == 8  # 基本 5 + macro/リスク管理/相談役
    finally:
        await eng.stop()
        await store.close()


async def test_supervised_noncritical_crash_does_not_halt(engine: Engine) -> None:
    """表示・助言系(critical=False)の連続障害は再起動のみで、取引本体を止めない。"""
    import asyncio as _asyncio

    real_sleep = _asyncio.sleep

    async def fast_sleep(_: float) -> None:
        await real_sleep(0)

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr("core.engine.asyncio.sleep", fast_sleep)
    try:
        async def always_broken() -> None:
            raise RuntimeError("boom")

        task = _asyncio.create_task(
            engine._supervised("相談役(テスト)", always_broken, critical=False)
        )
        for _ in range(100):
            await real_sleep(0.01)
            if len([m for m in engine.messages if m["type"] == "halt"]) > 0:  # type: ignore[attr-defined]
                break
        task.cancel()
        assert engine.gate.halted is False  # 何度落ちても緊急停止には倒さない
    finally:
        mp.undo()


async def test_supervised_repeated_crashes_halt_new_entries(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """短時間にクラッシュが連発したら安全側(新規停止)に倒す。"""
    real_sleep = asyncio.sleep

    async def fast_sleep(_: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("core.engine.asyncio.sleep", fast_sleep)

    async def always_broken() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(engine._supervised("壊れたタスク", always_broken))
    try:
        for _ in range(200):
            if engine.gate.halted:
                break
            await real_sleep(0.01)
    finally:
        task.cancel()
    assert engine.gate.halted is True
