"""リスク管理エージェント(F-20)。

経済指標カレンダー(F-19)・関連指標(F-18)・日次損益・連敗状況を監視し、
取引抑制モード(新規停止/サイズ半減)の発動・解除を判定する。

安全設計(CLAUDE.md):
- 判定はすべてルール(コード)。結果は RiskGate.restraint に状態として反映され、
  発注可否はコードが強制する。LLM が発注可否を直接決めることはない
- 根拠は日本語テキストで思考ログに出力する(発言エージェント名「リスク管理」)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config import RiskAgentConfig, RiskConfig
from core.models import EconomicEvent, RestraintMode, RestraintState

AGENT_NAME = "リスク管理"


@dataclass
class RiskInputs:
    """評価に使う入力のスナップショット。"""

    daily_realized_pnl_jpy: int = 0
    consecutive_losses_today: int = 0
    vix: float | None = None  # 鮮度切れ・未取得なら None(判定から除外)
    active_events: list[EconomicEvent] = field(default_factory=list)


def evaluate(
    inputs: RiskInputs, risk: RiskConfig, cfg: RiskAgentConfig
) -> RestraintState:
    """抑制モードを判定する(純関数)。理由は日本語で列挙し、最も強いモードを採用。"""
    no_entry: list[str] = []
    size_half: list[str] = []

    for ev in inputs.active_events:
        text = (
            f"{ev.label} の発表前後(前 {ev.window_before_min} 分/後 {ev.window_after_min} 分)"
            "の取引抑制ウィンドウ内"
        )
        if ev.importance == "hi":
            no_entry.append(text)
        else:
            size_half.append(text)

    if inputs.vix is not None:
        if inputs.vix >= cfg.vix_no_entry:
            no_entry.append(
                f"恐怖指数 VIX が {inputs.vix:.1f} と警戒水準({cfg.vix_no_entry:.0f})以上"
            )
        elif inputs.vix >= cfg.vix_size_half:
            size_half.append(
                f"恐怖指数 VIX が {inputs.vix:.1f} と注意水準({cfg.vix_size_half:.0f})以上"
            )

    if inputs.consecutive_losses_today >= cfg.consecutive_losses_size_half:
        size_half.append(f"本日 {inputs.consecutive_losses_today} 連敗中")

    warn_line = int(risk.max_daily_loss_jpy * cfg.daily_loss_warn_ratio)
    if inputs.daily_realized_pnl_jpy <= -warn_line:
        size_half.append(
            f"日次損失が上限(¥{risk.max_daily_loss_jpy:,})の"
            f" {cfg.daily_loss_warn_ratio:.0%} に接近(現在 "
            f"{inputs.daily_realized_pnl_jpy:+,} 円)"
        )

    if no_entry:
        return RestraintState(mode=RestraintMode.NO_ENTRY, reasons=no_entry + size_half)
    if size_half:
        return RestraintState(mode=RestraintMode.SIZE_HALF, reasons=size_half)
    return RestraintState()


MODE_LABEL: dict[RestraintMode, str] = {
    RestraintMode.NONE: "解除",
    RestraintMode.SIZE_HALF: "サイズ半減",
    RestraintMode.NO_ENTRY: "新規停止",
}


def transition_text(old: RestraintState, new: RestraintState) -> str:
    """抑制状態の変化を思考ログ用の日本語テキストにする。"""
    if new.mode is RestraintMode.NONE:
        return (
            "取引抑制モードを解除しました。抑制の要因"
            f"({' / '.join(old.reasons) or '—'})が解消したため、通常運転に戻します。"
        )
    action = {
        RestraintMode.SIZE_HALF: "新規エントリーのポジションサイズを半分に制限します",
        RestraintMode.NO_ENTRY: "新規エントリーを停止します(決済・損切り監視は継続)",
    }[new.mode]
    return f"取引抑制モード({MODE_LABEL[new.mode]})を発動: {' / '.join(new.reasons)}。{action}。"
