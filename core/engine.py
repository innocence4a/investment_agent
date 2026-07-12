"""エージェント・コアのオーケストレーション。

フィード → マーケット状態 → LLM 判断 → リスクゲート → ペーパーブローカー → 記録/配信
を非同期タスクとして常駐実行する。

安全設計:
- 発注は必ず RiskGate を通す(LLM 出力がどうであれコードで強制)
- 緊急停止状態はここ(コア)で保持し DB に永続化。ダッシュボードの生死に依存しない
- 損切り/利確の監視は LLM 判断サイクルとは独立にティック毎に実行(停止中も継続)
- フィード断(stale)を検知したら通知し、新規エントリーを見送る(N-2)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, timedelta, timezone
from typing import Any

from core.agent import AGENT_NAME, CostLimitExceeded, MockTraderAgent, TraderAgent
from core.agent import build_market_context as build_context
from core.broker import BrokerError, PaperBroker
from core.config import Settings
from core.feed import PriceFeed
from core.market import MarketState
from core.models import (
    SYMBOLS,
    Candle,
    Kpi,
    Thought,
    Ticker,
    TraderDecision,
    utcnow,
)
from core.notifier import Notifier
from core.risk import RiskGate
from core.store import Store

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


def _yen(v: int) -> str:
    return f"¥{v:,}"


class Engine:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        feed: PriceFeed,
        agent: TraderAgent,
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.store = store
        self.feed = feed
        self.agent = agent
        self.notifier = notifier
        self.market = MarketState()
        self.broker = PaperBroker(
            settings.start_capital_jpy,
            slippage_bps=settings.slippage_bps,
            fee_bps=settings.fee_bps,
        )
        self.gate = RiskGate(settings.risk)
        self.started_at = utcnow()
        self.monthly_llm_cost_usd = 0.0
        self._broadcast: Broadcast | None = None
        self._trade_lock = asyncio.Lock()
        self._dirty_candles: dict[tuple[str, str], Candle] = {}
        self._last_tick_broadcast: dict[str, float] = {}
        self._tasks: list[asyncio.Task[None]] = []

    def set_broadcast(self, fn: Broadcast) -> None:
        self._broadcast = fn

    async def broadcast(self, msg_type: str, data: Any) -> None:
        if self._broadcast is not None:
            await self._broadcast({"type": msg_type, "data": data})

    # ── 起動・停止 ─────────────────────────────────
    async def start(self) -> None:
        # 状態復元(緊急停止フラグ・ブローカー・ローソク足)
        self.gate.halted = (await self.store.get_state("halted", "0")) == "1"
        cash, positions = await self.store.load_broker()
        if cash is not None:
            self.broker.restore(cash, positions)
        for sym in SYMBOLS:
            for tf in list(self.market.candles[sym].keys()):
                self.market.prime(await self.store.load_candles(sym, tf))
        await self._add_thought(
            Thought(
                ts=utcnow(), agent="システム", kind="system",
                text=f"エージェント起動。{self.settings.mode.upper()} モードで BTC/ETH の監視を"
                f"開始します。リスク上限: 1取引 {_yen(self.settings.risk.max_trade_notional_jpy)}"
                f" / 日次損失 {_yen(self.settings.risk.max_daily_loss_jpy)}"
                f" / 総枠 {_yen(self.settings.risk.max_exposure_jpy)}。"
                + ("(緊急停止状態を引き継ぎました)" if self.gate.halted else ""),
            )
        )
        await self.notifier.send(
            f"エージェント・コア起動({self.settings.mode.upper()} / feed={self.settings.feed}"
            f" / llm={self.settings.llm})"
        )
        loop_fns: list[Callable[[], Coroutine[Any, Any, None]]] = [
            self._feed_loop, self._decision_loop, self._equity_loop,
            self._flush_loop, self._watchdog_loop,
        ]
        self._tasks = [asyncio.create_task(fn()) for fn in loop_fns]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self._flush_candles()
        await self.store.save_broker(self.broker.cash, self.broker.positions())
        await self.notifier.send("エージェント・コア停止", level="warn")

    # ── 緊急停止(F-13) ────────────────────────────
    async def set_halted(self, halted: bool, source: str = "ダッシュボード") -> None:
        if halted == self.gate.halted:
            return
        self.gate.halted = halted
        await self.store.set_state("halted", "1" if halted else "0")
        text = (
            f"{source}から緊急停止が実行されました。新規エントリーを停止します"
            "(既存ポジションの損切り・利確監視は継続)。"
            if halted
            else f"{source}から再開されました。次の判断サイクルから通常運転に戻ります。"
        )
        await self._add_thought(
            Thought(ts=utcnow(), agent="システム", kind="system", text=text)
        )
        await self.broadcast("halt", {"halted": halted})
        await self.notifier.send(text, level="warn" if halted else "info")

    # ── フィード消費 ────────────────────────────────
    async def _feed_loop(self) -> None:
        async for tick in self.feed.stream():
            updated = self.market.update(tick)
            for c in updated:
                self._dirty_candles[(c.symbol, c.tf.value)] = c
            await self._check_exits(tick)
            now = asyncio.get_running_loop().time()
            last = self._last_tick_broadcast.get(tick.symbol, 0.0)
            if now - last >= 0.3:  # 高頻度ティックの配信は 0.3 秒に間引く(N-1 の 1 秒以内は満たす)
                self._last_tick_broadcast[tick.symbol] = now
                await self.broadcast(
                    "tick",
                    {
                        "symbol": tick.symbol,
                        "price": tick.price,
                        "ts": tick.ts.isoformat(),
                        "candles": [c.model_dump() for c in updated],
                    },
                )

    # ── 損切り/利確の常時監視(LLM とは独立、停止中も継続) ──
    async def _check_exits(self, tick: Ticker) -> None:
        pos = self.broker.positions().get(tick.symbol)
        if pos is None:
            return
        signal = self.gate.exit_signal(pos, tick.price)
        if signal is None:
            return
        async with self._trade_lock:
            pos = self.broker.positions().get(tick.symbol)
            if pos is None or self.gate.exit_signal(pos, tick.price) is None:
                return
            try:
                fill = self.broker.close(tick.symbol, tick.price, utcnow())
            except BrokerError as e:
                logger.error("強制決済に失敗: %s", e)
                return
            fill = await self.store.add_fill(fill)
            c = self.settings.risk
            label = (
                f"損切りライン(取得比 −{c.stop_loss_pct}%)"
                if signal == "sl"
                else f"利確ライン(取得比 +{c.take_profit_pct}%)"
            )
            await self._add_thought(
                Thought(
                    ts=utcnow(), agent="リスクゲート", kind="sell", symbol=tick.symbol,
                    text=f"{label}に到達したため、コード側の常時監視により {tick.symbol} を"
                    f"全量決済しました(実現損益 {_yen(fill.realized_pnl or 0)})。",
                    gate=f"発動({'損切り' if signal == 'sl' else '利確'})",
                )
            )
            await self._after_fill_broadcast(fill)
            await self.notifier.send(
                f"{label}発動: {tick.symbol} 決済 実現損益 {_yen(fill.realized_pnl or 0)}",
                level="trade",
            )

    # ── LLM 判断サイクル(F-2) ─────────────────────
    async def _decision_loop(self) -> None:
        # 起動直後はローソク足が溜まるまで少し待つ
        await asyncio.sleep(min(10.0, self.settings.decision_interval_sec))
        while True:
            try:
                await self._decide_once()
            except CostLimitExceeded as e:
                await self._add_thought(
                    Thought(
                        ts=utcnow(), agent="システム", kind="system",
                        text=f"{e} — 判断サイクルを一時停止します(コスト上限は月替わりで解除)。",
                    )
                )
                await self.notifier.send(str(e), level="error")
                await asyncio.sleep(3600)
            except Exception:
                logger.exception("判断サイクルでエラー")
                await self.notifier.send("判断サイクルでエラーが発生しました", level="error")
            await asyncio.sleep(self.settings.decision_interval_sec)

    async def _decide_once(self) -> None:
        if not self.market.last_price:
            return
        if self._feed_stale():
            await self._add_thought(
                Thought(
                    ts=utcnow(), agent="システム", kind="system",
                    text="価格フィードが途絶しているため、今サイクルの判断を見送ります"
                    "(障害時は新規発注停止に倒す方針)。",
                )
            )
            return
        daily_pnl, trades_today = await self._daily_stats()
        context = build_context(
            self.market, self.broker.positions(),
            cash=self.broker.cash, daily_pnl=daily_pnl, trades_today=trades_today,
            max_trade_notional=self.settings.risk.max_trade_notional_jpy,
        )
        decision = await self.agent.decide(context)
        await self._apply_decision(decision, daily_pnl, trades_today)

    async def _apply_decision(
        self, d: TraderDecision, daily_pnl: int, trades_today: int
    ) -> None:
        async with self._trade_lock:
            price = self.market.last_price.get(d.symbol)
            if d.action == "hold" or price is None:
                await self._add_thought(
                    Thought(
                        ts=utcnow(), agent=AGENT_NAME, kind="skip", symbol=d.symbol,
                        text=d.reason, confidence=d.confidence, gate="—",
                    )
                )
                return
            if d.action == "buy":
                gate = self.gate.check_entry(
                    notional_jpy=d.notional_jpy,
                    exposure_jpy=self.broker.exposure(self.market.last_price),
                    daily_realized_pnl_jpy=daily_pnl,
                    trades_today=trades_today,
                    cash_jpy=self.broker.cash,
                )
                if not gate.allowed:
                    await self._add_thought(
                        Thought(
                            ts=utcnow(), agent="リスクゲート", kind="risk", symbol=d.symbol,
                            text=f"トレーダーの買い提案({_yen(d.notional_jpy)})を拒否: "
                            f"{gate.reason}",
                            gate=f"発動({gate.code})",
                        )
                    )
                    await self.notifier.send(
                        f"リスクゲート発動({gate.code}): {gate.reason}", level="warn"
                    )
                    return
                try:
                    fill = self.broker.buy(d.symbol, d.notional_jpy, price, utcnow())
                except BrokerError as e:
                    await self._add_thought(
                        Thought(
                            ts=utcnow(), agent="システム", kind="system", symbol=d.symbol,
                            text=f"発注に失敗しました: {e}",
                        )
                    )
                    return
                fill = await self.store.add_fill(fill)
                await self._add_thought(
                    Thought(
                        ts=utcnow(), agent=AGENT_NAME, kind="buy", symbol=d.symbol,
                        text=d.reason, confidence=d.confidence, gate="通過",
                    )
                )
                await self._after_fill_broadcast(fill)
                await self.notifier.send(
                    f"買い約定: {d.symbol} {fill.qty} @ {_yen(fill.price)}"
                    f"(約 {_yen(fill.notional)})",
                    level="trade",
                )
            elif d.action == "close":
                gate = self.gate.check_close(
                    has_position=d.symbol in self.broker.positions()
                )
                if not gate.allowed:
                    await self._add_thought(
                        Thought(
                            ts=utcnow(), agent=AGENT_NAME, kind="skip", symbol=d.symbol,
                            text=f"{d.reason} → ただし {gate.reason}", confidence=d.confidence,
                            gate=f"拒否({gate.code})",
                        )
                    )
                    return
                try:
                    fill = self.broker.close(d.symbol, price, utcnow())
                except BrokerError as e:
                    logger.error("決済に失敗: %s", e)
                    return
                fill = await self.store.add_fill(fill)
                await self._add_thought(
                    Thought(
                        ts=utcnow(), agent=AGENT_NAME, kind="sell", symbol=d.symbol,
                        text=d.reason, confidence=d.confidence, gate="通過",
                    )
                )
                await self._after_fill_broadcast(fill)
                await self.notifier.send(
                    f"決済約定: {d.symbol} 実現損益 {_yen(fill.realized_pnl or 0)}",
                    level="trade",
                )

    # ── 集計・スナップショット ──────────────────────
    def _jst_midnight_utc_iso(self) -> str:
        now_jst = utcnow().astimezone(JST)
        midnight = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(UTC).isoformat()

    async def _daily_stats(self) -> tuple[int, int]:
        """(本日の実現損益, 本日の約定回数)。「本日」は JST の暦日。"""
        fills = await self.store.fills_since(self._jst_midnight_utc_iso())
        pnl = sum(f.realized_pnl or 0 for f in fills)
        return pnl, len(fills)

    async def kpi(self) -> Kpi:
        daily_pnl, _ = await self._daily_stats()
        wins, losses = await self.store.win_loss_counts()
        equity = self.broker.equity(self.market.last_price)
        if isinstance(self.agent, MockTraderAgent):
            cost = 0.0
        else:
            cost = await self.agent.monthly_cost_usd()  # type: ignore[attr-defined]
        self.monthly_llm_cost_usd = cost
        return Kpi(
            equity=equity,
            cash=self.broker.cash,
            start_capital=self.settings.start_capital_jpy,
            pnl_total=equity - self.settings.start_capital_jpy,
            pnl_today=daily_pnl,
            wins=wins,
            losses=losses,
            trades=await self.store.trade_count(),
            started_at=self.started_at,
            halted=self.gate.halted,
            monthly_llm_cost_usd=cost,
        )

    async def _after_fill_broadcast(self, fill: Any) -> None:
        await self.store.save_broker(self.broker.cash, self.broker.positions())
        await self.broadcast("fill", fill.model_dump())
        await self.broadcast(
            "positions", [p.model_dump() for p in self.broker.positions().values()]
        )
        kpi = await self.kpi()
        await self.broadcast("kpi", kpi.model_dump())
        eq = self.broker.equity(self.market.last_price)
        await self.store.add_equity_snapshot(utcnow(), eq, self.broker.cash)
        await self.broadcast("equity", {"ts": utcnow().isoformat(), "equity": eq})

    async def _add_thought(self, t: Thought) -> None:
        t = await self.store.add_thought(t)
        await self.broadcast("thought", t.model_dump())

    async def _equity_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            if not self.market.last_price:
                continue
            eq = self.broker.equity(self.market.last_price)
            await self.store.add_equity_snapshot(utcnow(), eq, self.broker.cash)
            await self.broadcast("equity", {"ts": utcnow().isoformat(), "equity": eq})
            kpi = await self.kpi()
            await self.broadcast("kpi", kpi.model_dump())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._flush_candles()

    async def _flush_candles(self) -> None:
        dirty = list(self._dirty_candles.values())
        self._dirty_candles.clear()
        for c in dirty:
            await self.store.upsert_candle(c)

    # ── フィード死活監視(F-7 / N-2) ────────────────
    def _feed_stale(self) -> bool:
        if not self.market.last_ts:
            return True
        latest = max(self.market.last_ts.values())
        return (utcnow() - latest).total_seconds() > self.settings.feed_stale_sec

    async def _watchdog_loop(self) -> None:
        notified = False
        # 起動直後の未受信は猶予する
        await asyncio.sleep(self.settings.feed_stale_sec)
        while True:
            await asyncio.sleep(10)
            stale = self._feed_stale()
            if stale and not notified:
                notified = True
                await self.notifier.send(
                    f"価格フィードが {self.settings.feed_stale_sec:.0f} 秒以上途絶しています。"
                    "新規エントリーを停止して回復を待ちます。",
                    level="error",
                )
            elif not stale and notified:
                notified = False
                await self.notifier.send("価格フィードが回復しました。")

    # ── ダッシュボード初期スナップショット ──────────
    async def snapshot(self) -> dict[str, Any]:
        kpi = await self.kpi()
        candles: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for sym in SYMBOLS:
            candles[sym] = {
                tf.value: [c.model_dump() for c in arr[-160:]]
                for tf, arr in self.market.candles[sym].items()
            }
        return {
            "config": {
                "mode": self.settings.mode.upper(),
                "feed": self.settings.feed,
                "llm": self.settings.llm,
                "decision_interval_sec": self.settings.decision_interval_sec,
                "symbols": list(SYMBOLS),
            },
            "kpi": kpi.model_dump(),
            "halted": self.gate.halted,
            "prices": dict(self.market.last_price),
            "candles": candles,
            "equity": [e.model_dump() for e in await self.store.recent_equity(300)],
            "thoughts": [t.model_dump() for t in await self.store.recent_thoughts(50)],
            "fills": [f.model_dump() for f in await self.store.recent_fills(30)],
            "positions": [p.model_dump() for p in self.broker.positions().values()],
        }
