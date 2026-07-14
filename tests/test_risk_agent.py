"""リスク管理エージェント(F-20)のルール評価テスト(境界値含む)。"""

from datetime import UTC, datetime

from core.config import RiskAgentConfig, RiskConfig
from core.models import EconomicEvent, RestraintMode
from core.risk_agent import RiskInputs, evaluate, transition_text

RISK = RiskConfig(max_daily_loss_jpy=30_000)
CFG = RiskAgentConfig(
    vix_size_half=25.0,
    vix_no_entry=35.0,
    consecutive_losses_size_half=3,
    daily_loss_warn_ratio=0.8,
)


def event(importance: str = "hi") -> EconomicEvent:
    return EconomicEvent(
        id="e", label="米CPI",
        importance=importance,  # type: ignore[arg-type]
        ts=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        window_before_min=30, window_after_min=30,
    )


def test_normal_returns_none() -> None:
    state = evaluate(RiskInputs(), RISK, CFG)
    assert state.mode is RestraintMode.NONE
    assert state.reasons == []


def test_hi_event_window_triggers_no_entry() -> None:
    state = evaluate(RiskInputs(active_events=[event("hi")]), RISK, CFG)
    assert state.mode is RestraintMode.NO_ENTRY
    assert any("米CPI" in r for r in state.reasons)


def test_mid_event_window_triggers_size_half() -> None:
    state = evaluate(RiskInputs(active_events=[event("mid")]), RISK, CFG)
    assert state.mode is RestraintMode.SIZE_HALF


def test_vix_boundaries() -> None:
    assert evaluate(RiskInputs(vix=24.9), RISK, CFG).mode is RestraintMode.NONE
    assert evaluate(RiskInputs(vix=25.0), RISK, CFG).mode is RestraintMode.SIZE_HALF
    assert evaluate(RiskInputs(vix=34.9), RISK, CFG).mode is RestraintMode.SIZE_HALF
    assert evaluate(RiskInputs(vix=35.0), RISK, CFG).mode is RestraintMode.NO_ENTRY


def test_vix_none_is_ignored() -> None:
    # 鮮度切れ・未取得の VIX では判定しない(誤発動防止)
    assert evaluate(RiskInputs(vix=None), RISK, CFG).mode is RestraintMode.NONE


def test_consecutive_losses_boundary() -> None:
    assert evaluate(RiskInputs(consecutive_losses_today=2), RISK, CFG).mode is RestraintMode.NONE
    state = evaluate(RiskInputs(consecutive_losses_today=3), RISK, CFG)
    assert state.mode is RestraintMode.SIZE_HALF
    assert any("3 連敗" in r for r in state.reasons)


def test_daily_loss_warn_boundary() -> None:
    # 上限 30,000 × 0.8 = 24,000 ちょうどで発動
    assert (
        evaluate(RiskInputs(daily_realized_pnl_jpy=-23_999), RISK, CFG).mode
        is RestraintMode.NONE
    )
    assert (
        evaluate(RiskInputs(daily_realized_pnl_jpy=-24_000), RISK, CFG).mode
        is RestraintMode.SIZE_HALF
    )


def test_no_entry_takes_precedence_and_collects_all_reasons() -> None:
    state = evaluate(
        RiskInputs(vix=26.0, consecutive_losses_today=3, active_events=[event("hi")]),
        RISK, CFG,
    )
    assert state.mode is RestraintMode.NO_ENTRY
    assert len(state.reasons) == 3  # イベント + VIX + 連敗 をすべて列挙


def test_transition_text_activation_and_release() -> None:
    from core.models import RestraintState

    activated = RestraintState(mode=RestraintMode.NO_ENTRY, reasons=["米CPI の発表前後"])
    text = transition_text(RestraintState(), activated)
    assert "新規停止" in text
    assert "米CPI" in text
    released = transition_text(activated, RestraintState())
    assert "解除" in text or "解除" in released
    assert "通常運転" in released
