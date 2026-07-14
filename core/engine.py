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

from core import risk_agent
from core.advisor import AGENT_NAME as ADVISOR_NAME
from core.advisor import AdvisorAgent
from core.agent import AGENT_NAME, CostLimitExceeded, MockTraderAgent, TraderAgent
from core.agent import build_market_context as build_context
from core.broker import BrokerError, PaperBroker
from core.calendar import EconomicCalendar
from core.config import Settings
from core.feed import PriceFeed
from core.macro import MacroSource
from core.market import MarketState
from core.models import (
    SYMBOLS,
    Candle,
    Kpi,
    MacroSnapshot,
    Symbol,
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
        *,
        macro_source: MacroSource | None = None,
        calendar: EconomicCalendar | None = None,
        advisor: AdvisorAgent | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.feed = feed
        self.agent = agent
        self.notifier = notifier
        self.macro_source = macro_source
        self.calendar = calendar
        self.advisor = advisor
        self.macro = MacroSnapshot()
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
        loop_fns: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
            ("フィード消費・損切り監視", self._feed_loop),
            ("LLM 判断サイクル", self._decision_loop),
            ("資産スナップショット", self._equity_loop),
            ("ローソク足永続化", self._flush_loop),
            ("フィード死活監視", self._watchdog_loop),
        ]
        if self.macro_source is not None:
            loop_fns.append(("関連指標の取得", self._macro_loop))
        if self.calendar is not None:
            loop_fns.append(("リスク管理エージェント", self._risk_agent_loop))
        if self.advisor is not None:
            loop_fns.append(("相談役エージェント", self._advisor_loop))
        self._tasks = [
            asyncio.create_task(self._supervised(name, fn)) for name, fn in loop_fns
        ]

    async def _supervised(self, name: str, fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """バックグラウンドタスクの監督: 未捕捉例外を通知して再起動する。

        損切り監視(_feed_loop)等が単一の例外で無言停止しないための安全装置。
        短時間に連続してクラッシュする場合は障害とみなし、新規発注停止に倒す(N-2)。
        """
        failures = 0
        last_failure = 0.0
        while True:
            try:
                await fn()
                return  # 常駐ループが正常 return することは通常ない
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("バックグラウンドタスク「%s」が例外で停止", name)
                now = asyncio.get_running_loop().time()
                failures = failures + 1 if now - last_failure < 120 else 1
                last_failure = now
                await self.notifier.send(
                    f"バックグラウンドタスク「{name}」が例外で停止しました。"
                    f"再起動します(直近 {failures} 回目)。",
                    level="error",
                )
                if failures >= 3 and not self.gate.halted:
                    await self.set_halted(True, source=f"システム(タスク「{name}」の連続障害)")
                await asyncio.sleep(min(2.0**failures, 30.0))

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
        stale = self._stale_symbols()
        if len(stale) == len(SYMBOLS):
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
            # 抑制(サイズ半減)中は LLM に伝える上限も半分にする(ゲートは別途強制)
            max_trade_notional=self.gate.effective_max_trade_notional(),
            exclude=stale,  # 途絶中の銘柄は判断材料から除外(古い価格で判断させない)
        )
        context.update(self._trader_extra_context())
        decision = await self.agent.decide(context)
        await self._apply_decision(decision)

    def _trader_extra_context(self) -> dict[str, Any]:
        """関連指標(F-18)・カレンダー(F-19)・抑制状態を LLM 判断のインプットに加える。"""
        extra: dict[str, Any] = {}
        macro = self._fresh_macro()
        if macro:
            extra["macro_indicators"] = {
                key: {"value": t.value, "change_pct": t.change_pct} for key, t in macro.items()
            }
        if self.calendar is not None:
            now = utcnow()
            extra["upcoming_events"] = [
                {
                    "label": e.label,
                    "importance": e.importance,
                    "minutes_until": int((e.ts - now).total_seconds() // 60),
                }
                for e in self.calendar.upcoming(now, limit=3)
            ]
        r = self.gate.restraint
        extra["restraint"] = {"mode": r.mode.value, "reasons": r.reasons}
        return extra

    async def _apply_decision(self, d: TraderDecision) -> None:
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
            # LLM 応答待ちの間にフィードが途絶した銘柄は、古い価格での約定を拒否する
            if d.symbol in self._stale_symbols():
                await self._add_thought(
                    Thought(
                        ts=utcnow(), agent="リスクゲート", kind="risk", symbol=d.symbol,
                        text=f"{d.symbol} の価格フィードが途絶しているため、"
                        "古い価格での発注を拒否しました(回復後に再判断します)。",
                        gate="発動(FEED_STALE)",
                    )
                )
                return
            if d.action == "buy":
                # 日次損益・取引回数は LLM 応答待ちの間に変わり得る(損切り約定等)ため、
                # 発注直前・ロック内で必ず取り直してからゲートを通す
                daily_pnl, trades_today = await self._daily_stats()
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
    def _stale_symbols(self) -> set[Symbol]:
        """フィードが途絶している銘柄の集合(銘柄別に判定)。"""
        now = utcnow()
        return {
            s
            for s in SYMBOLS
            if s not in self.market.last_ts
            or (now - self.market.last_ts[s]).total_seconds() > self.settings.feed_stale_sec
        }

    async def _watchdog_loop(self) -> None:
        notified: set[Symbol] = set()
        # 起動直後の未受信は猶予する
        await asyncio.sleep(self.settings.feed_stale_sec)
        while True:
            await asyncio.sleep(10)
            stale = self._stale_symbols()
            for sym in stale - notified:
                await self.notifier.send(
                    f"{sym} の価格フィードが {self.settings.feed_stale_sec:.0f} 秒以上"
                    "途絶しています。当該銘柄の新規エントリーを停止して回復を待ちます。",
                    level="error",
                )
            for sym in notified - stale:
                await self.notifier.send(f"{sym} の価格フィードが回復しました。")
            notified = stale

    # ── 関連指標(F-18) ─────────────────────────────
    async def _macro_loop(self) -> None:
        assert self.macro_source is not None
        while True:
            snap = await self.macro_source.fetch()
            if snap.tiles:  # 全滅時は古いスナップショットを保持(鮮度は表示側で判断)
                self.macro = snap
                await self.broadcast("macro", snap.model_dump())
            await asyncio.sleep(self.settings.macro_poll_sec)

    def _fresh_macro(self) -> dict[str, Any]:
        """鮮度切れしていない関連指標のみを返す(古い値で判断させない)。"""
        now = utcnow()
        return {
            key: t
            for key, t in self.macro.tiles.items()
            if (now - t.ts).total_seconds() <= self.settings.macro_stale_sec
        }

    def _fresh_vix(self) -> float | None:
        tile = self._fresh_macro().get("vix")
        return float(tile.value) if tile is not None else None

    # ── リスク管理エージェント(F-20) ────────────────
    async def _consecutive_losses_today(self) -> int:
        fills = await self.store.fills_since(self._jst_midnight_utc_iso())
        streak = 0
        for f in reversed(fills):
            if f.realized_pnl is None:
                continue  # 買い約定は連敗の判定対象外
            if f.realized_pnl < 0:
                streak += 1
            else:
                break
        return streak

    async def _risk_agent_loop(self) -> None:
        assert self.calendar is not None
        while True:
            await self._evaluate_restraint()
            await asyncio.sleep(self.settings.risk_agent.check_interval_sec)

    async def _evaluate_restraint(self) -> None:
        """ルール評価の結果をリスクゲートの状態に反映する(変化時のみ記録・通知)。"""
        assert self.calendar is not None
        daily_pnl, _ = await self._daily_stats()
        inputs = risk_agent.RiskInputs(
            daily_realized_pnl_jpy=daily_pnl,
            consecutive_losses_today=await self._consecutive_losses_today(),
            vix=self._fresh_vix(),
            active_events=self.calendar.active_events(),
        )
        new = risk_agent.evaluate(inputs, self.settings.risk, self.settings.risk_agent)
        old = self.gate.restraint
        if new == old:
            return
        self.gate.restraint = new
        text = risk_agent.transition_text(old, new)
        await self._add_thought(
            Thought(
                ts=utcnow(), agent=risk_agent.AGENT_NAME, kind="risk", text=text,
                gate=f"抑制({risk_agent.MODE_LABEL[new.mode]})",
            )
        )
        await self.broadcast("restraint", new.model_dump())
        await self.notifier.send(
            f"リスク管理: {text}", level="warn" if new.reasons else "info"
        )

    # ── 相談役エージェント(F-21) ────────────────────
    async def _advisor_loop(self) -> None:
        assert self.advisor is not None
        # 起動時: 直近 20 時間以内にレビューが無ければ 1 回実行(再起動での取りこぼし防止)
        await asyncio.sleep(15)
        last = await self.store.last_thought_ts("advice")
        if last is None or (utcnow() - last).total_seconds() > 20 * 3600:
            await self._run_advisor()
        while True:
            await asyncio.sleep(self._seconds_until_advisor_hour())
            await self._run_advisor()

    def _seconds_until_advisor_hour(self) -> float:
        """次の実行時刻(JST の advisor_hour_jst 時)までの秒数。"""
        now = utcnow().astimezone(JST)
        target = now.replace(
            hour=self.settings.advisor_hour_jst, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return max((target - now).total_seconds(), 60.0)

    async def _run_advisor(self) -> None:
        """日次レビューを実行し、思考ログと Slack(モーニングレポート)に出力する。

        相談役は助言のみ(発注経路に接続しない)。失敗しても取引系タスクに影響しない。
        """
        assert self.advisor is not None
        kpi = await self.kpi()
        now = utcnow()
        recent_thoughts = await self.store.recent_thoughts(30)
        context: dict[str, Any] = {
            "kpi": {
                "equity": kpi.equity, "pnl_total": kpi.pnl_total,
                "pnl_today": kpi.pnl_today, "wins": kpi.wins, "losses": kpi.losses,
                "trades": kpi.trades,
            },
            "macro": {
                key: {"value": t.value, "change_pct": t.change_pct}
                for key, t in self._fresh_macro().items()
            },
            "upcoming_events": [
                {
                    "label": e.label,
                    "importance": e.importance,
                    "minutes_until": int((e.ts - now).total_seconds() // 60),
                }
                for e in (self.calendar.upcoming(now, limit=5) if self.calendar else [])
            ],
            "restraint": {
                "mode": self.gate.restraint.mode.value,
                "reasons": self.gate.restraint.reasons,
            },
            "recent_decisions": [
                {"kind": t.kind, "agent": t.agent, "text": t.text[:120]}
                for t in recent_thoughts
                if t.kind in ("buy", "sell", "skip", "risk")
            ][-15:],
        }
        try:
            text = await self.advisor.review(context)
        except Exception as e:
            logger.warning("相談役レビューに失敗: %s", e)
            await self.notifier.send(f"相談役レビューに失敗しました: {e}", level="warn")
            return
        await self._add_thought(
            Thought(ts=utcnow(), agent=ADVISOR_NAME, kind="advice", text=text)
        )
        await self.notifier.send(f"相談役の日次レビュー:\n{text}")

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
            "macro": self.macro.model_dump(),
            "calendar": [
                e.model_dump()
                for e in (self.calendar.upcoming(limit=6) if self.calendar else [])
            ],
            "restraint": self.gate.restraint.model_dump(),
        }
