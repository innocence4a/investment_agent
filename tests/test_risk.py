"""リスクゲート(F-3)のテスト。

品質ゲート要件: 全制約について「上限ちょうど・超過・境界値」+キルスイッチ発動時の挙動。
"""

from decimal import Decimal

from core.config import RiskConfig
from core.models import Position
from core.risk import RiskGate

CFG = RiskConfig(
    max_trade_notional_jpy=50_000,
    max_daily_loss_jpy=30_000,
    max_exposure_jpy=200_000,
    max_trades_per_day=30,
    stop_loss_pct=2.0,
    take_profit_pct=4.0,
)


def entry(gate: RiskGate, **overrides: int) -> tuple[bool, str]:
    kwargs: dict[str, int] = {
        "notional_jpy": 10_000,
        "exposure_jpy": 0,
        "daily_realized_pnl_jpy": 0,
        "trades_today": 0,
        "cash_jpy": 1_000_000,
    }
    kwargs.update(overrides)
    r = gate.check_entry(**kwargs)
    return r.allowed, r.code


class TestPerTradeLimit:
    def test_exact_limit_allowed(self) -> None:
        assert entry(RiskGate(CFG), notional_jpy=50_000) == (True, "PASS")

    def test_over_limit_rejected(self) -> None:
        assert entry(RiskGate(CFG), notional_jpy=50_001) == (False, "PER_TRADE_LIMIT")

    def test_zero_and_negative_rejected(self) -> None:
        assert entry(RiskGate(CFG), notional_jpy=0) == (False, "INVALID")
        assert entry(RiskGate(CFG), notional_jpy=-1) == (False, "INVALID")


class TestDailyLoss:
    def test_below_limit_allowed(self) -> None:
        assert entry(RiskGate(CFG), daily_realized_pnl_jpy=-29_999) == (True, "PASS")

    def test_at_limit_rejected(self) -> None:
        # 上限「到達」で新規停止(-30,000 ちょうどで発動)
        assert entry(RiskGate(CFG), daily_realized_pnl_jpy=-30_000) == (False, "DAILY_LOSS")

    def test_over_limit_rejected(self) -> None:
        assert entry(RiskGate(CFG), daily_realized_pnl_jpy=-30_001) == (False, "DAILY_LOSS")

    def test_profit_day_allowed(self) -> None:
        assert entry(RiskGate(CFG), daily_realized_pnl_jpy=50_000) == (True, "PASS")


class TestExposure:
    def test_exact_limit_allowed(self) -> None:
        assert entry(RiskGate(CFG), exposure_jpy=150_000, notional_jpy=50_000) == (True, "PASS")

    def test_over_limit_rejected(self) -> None:
        assert entry(RiskGate(CFG), exposure_jpy=150_001, notional_jpy=50_000) == (
            False,
            "EXPOSURE",
        )


class TestFrequency:
    def test_below_limit_allowed(self) -> None:
        assert entry(RiskGate(CFG), trades_today=29) == (True, "PASS")

    def test_at_limit_rejected(self) -> None:
        assert entry(RiskGate(CFG), trades_today=30) == (False, "FREQUENCY")


class TestCash:
    def test_exact_cash_allowed(self) -> None:
        assert entry(RiskGate(CFG), notional_jpy=10_000, cash_jpy=10_000) == (True, "PASS")

    def test_insufficient_cash_rejected(self) -> None:
        assert entry(RiskGate(CFG), notional_jpy=10_000, cash_jpy=9_999) == (
            False,
            "INSUFFICIENT_CASH",
        )


class TestKillSwitch:
    """緊急停止(F-13): 新規エントリー拒否、決済・損切り監視は継続。"""

    def test_halted_rejects_entry(self) -> None:
        gate = RiskGate(CFG, halted=True)
        assert entry(gate) == (False, "HALTED")

    def test_halted_allows_close(self) -> None:
        gate = RiskGate(CFG, halted=True)
        assert gate.check_close(has_position=True).allowed is True

    def test_halted_keeps_exit_monitoring(self) -> None:
        gate = RiskGate(CFG, halted=True)
        pos = Position(symbol="BTC_JPY", qty=Decimal("0.001"), avg_cost=Decimal(10_000_000))
        # 損切りライン(-2% = 9,800,000)以下で発動
        assert gate.exit_signal(pos, 9_800_000) == "sl"

    def test_resume_allows_entry(self) -> None:
        gate = RiskGate(CFG, halted=True)
        gate.halted = False
        assert entry(gate) == (True, "PASS")


class TestCloseGate:
    def test_no_position_rejected(self) -> None:
        gate = RiskGate(CFG)
        assert gate.check_close(has_position=False).code == "NO_POSITION"


class TestExitSignal:
    POS = Position(symbol="BTC_JPY", qty=Decimal("0.001"), avg_cost=Decimal(10_000_000))

    def test_stop_loss_boundary(self) -> None:
        gate = RiskGate(CFG)
        assert gate.exit_signal(self.POS, 9_800_001) is None  # ライン未満(1円上)
        assert gate.exit_signal(self.POS, 9_800_000) == "sl"  # ちょうど
        assert gate.exit_signal(self.POS, 9_700_000) == "sl"  # 超過

    def test_take_profit_boundary(self) -> None:
        gate = RiskGate(CFG)
        assert gate.exit_signal(self.POS, 10_399_999) is None
        assert gate.exit_signal(self.POS, 10_400_000) == "tp"

    def test_no_signal_in_range(self) -> None:
        gate = RiskGate(CFG)
        assert gate.exit_signal(self.POS, 10_000_000) is None

    def test_zero_position_no_signal(self) -> None:
        gate = RiskGate(CFG)
        empty = Position(symbol="BTC_JPY", qty=Decimal(0), avg_cost=Decimal(0))
        assert gate.exit_signal(empty, 1) is None
