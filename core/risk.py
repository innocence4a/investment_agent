"""リスクゲート(F-3)。

安全ルール(CLAUDE.md)の実装:
- すべての制約は LLM の出力に関係なく、発注前にコードで検査する
- 緊急停止(F-13)の状態はここ(エージェント・コア側)で保持する。
  停止中は新規エントリーを拒否するが、保有ポジションの決済(損切り監視含む)は継続する
- 境界の扱い: 「上限ちょうど」は許可(≦ 上限)、超過は拒否。
  日次損失・取引回数は「上限に到達した時点」で新規を停止する
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from core.config import RiskConfig
from core.models import GateResult, Position


def _yen(v: int) -> str:
    return f"¥{v:,}"


class RiskGate:
    """発注前チェックと損切り/利確の常時監視。緊急停止状態の保持者。"""

    def __init__(self, config: RiskConfig, *, halted: bool = False) -> None:
        self.config = config
        self.halted = halted

    # ── 新規エントリー ────────────────────────────────
    def check_entry(
        self,
        *,
        notional_jpy: int,
        exposure_jpy: int,
        daily_realized_pnl_jpy: int,
        trades_today: int,
        cash_jpy: int,
    ) -> GateResult:
        """新規エントリー(buy)の可否。すべての制約を検査し、最初の違反で拒否する。"""
        c = self.config
        if self.halted:
            return GateResult(
                allowed=False, code="HALTED",
                reason="緊急停止中のため新規エントリーを拒否しました(決済監視は継続)。",
            )
        if notional_jpy <= 0:
            return GateResult(allowed=False, code="INVALID", reason="発注額が不正です。")
        if notional_jpy > c.max_trade_notional_jpy:
            return GateResult(
                allowed=False, code="PER_TRADE_LIMIT",
                reason=f"1取引上限({_yen(c.max_trade_notional_jpy)})を超える発注"
                f"({_yen(notional_jpy)})を拒否しました。",
            )
        if daily_realized_pnl_jpy <= -c.max_daily_loss_jpy:
            return GateResult(
                allowed=False, code="DAILY_LOSS",
                reason=f"日次損失が上限({_yen(c.max_daily_loss_jpy)})に到達しているため、"
                "新規エントリーを拒否しました。本日は決済のみ行います。",
            )
        if exposure_jpy + notional_jpy > c.max_exposure_jpy:
            return GateResult(
                allowed=False, code="EXPOSURE",
                reason=f"総エクスポージャ上限({_yen(c.max_exposure_jpy)})を超えるため"
                f"拒否しました(現在 {_yen(exposure_jpy)} + 新規 {_yen(notional_jpy)})。",
            )
        if trades_today >= c.max_trades_per_day:
            return GateResult(
                allowed=False, code="FREQUENCY",
                reason=f"取引頻度が上限({c.max_trades_per_day} 回/日)に到達したため、"
                "本日の新規エントリーを停止します。",
            )
        if notional_jpy > cash_jpy:
            return GateResult(
                allowed=False, code="INSUFFICIENT_CASH",
                reason=f"現金残高({_yen(cash_jpy)})を超える発注を拒否しました。",
            )
        return GateResult(allowed=True, code="PASS", reason="通過")

    # ── 決済 ─────────────────────────────────────────
    def check_close(self, *, has_position: bool) -> GateResult:
        """決済(sell)の可否。緊急停止中・上限到達後も決済は常に許可する
        (ポジションを閉じられない状態はリスクを増やすため)。"""
        if not has_position:
            return GateResult(
                allowed=False, code="NO_POSITION", reason="決済対象のポジションがありません。"
            )
        return GateResult(allowed=True, code="PASS", reason="通過")

    # ── 損切り/利確の常時監視(LLM 判断サイクルとは独立) ──
    def exit_signal(self, position: Position, price: int) -> str | None:
        """損切り/利確ラインへの接触を判定する。"sl" / "tp" / None を返す。
        緊急停止中もこの監視は動き続ける(F-13)。"""
        if position.qty <= 0:
            return None
        c = self.config
        sl_line = position.avg_cost * (Decimal("1") - Decimal(str(c.stop_loss_pct)) / 100)
        tp_line = position.avg_cost * (Decimal("1") + Decimal(str(c.take_profit_pct)) / 100)
        p = Decimal(price)
        if p <= sl_line.quantize(Decimal("1"), ROUND_HALF_UP):
            return "sl"
        if p >= tp_line.quantize(Decimal("1"), ROUND_HALF_UP):
            return "tp"
        return None
