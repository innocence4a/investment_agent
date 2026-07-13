"""ペーパーブローカー(F-4)のテスト: 約定・残高・損益(端数・スリッページ・手数料)。"""

from decimal import Decimal

import pytest

from core.broker import BrokerError, PaperBroker
from core.models import Position, Symbol, utcnow


def test_buy_applies_slippage_and_fee() -> None:
    b = PaperBroker(1_000_000, slippage_bps=10, fee_bps=15)  # スリッページ 0.10%
    fill = b.buy("BTC_JPY", 100_000, 10_000_000, utcnow())
    assert fill.price == 10_010_000  # 買いは不利方向(高く)に約定
    # 100,000 / 10,010,000 = 0.0099900099… → 8 桁切り捨て(過大注文防止)
    assert fill.qty == Decimal("0.00999000")
    assert fill.notional == 100_000  # qty × price(=99,999.9)の四捨五入
    assert fill.fee == 150  # 0.15%
    assert b.cash == 1_000_000 - fill.notional - fill.fee


def test_close_realizes_pnl_after_fees() -> None:
    b = PaperBroker(1_000_000, slippage_bps=0, fee_bps=0)
    b.buy("BTC_JPY", 100_000, 10_000_000, utcnow())
    fill = b.close("BTC_JPY", 11_000_000, utcnow())  # +10%
    assert fill.side == "sell"
    assert fill.realized_pnl is not None and fill.realized_pnl > 0
    # 現金 = 開始資金 + 実現損益(スリッページ・手数料ゼロのため)
    assert b.cash == 1_000_000 + fill.realized_pnl
    assert b.positions() == {}


def test_sell_slippage_hurts_seller() -> None:
    b = PaperBroker(1_000_000, slippage_bps=10, fee_bps=0)
    b.buy("BTC_JPY", 100_000, 10_000_000, utcnow())
    fill = b.close("BTC_JPY", 10_000_000, utcnow())
    assert fill.price == 9_990_000  # 売りは安く約定
    assert fill.realized_pnl is not None and fill.realized_pnl < 0  # 往復スリッページ分の損


def test_round_trip_zero_slippage_zero_fee_preserves_cash() -> None:
    b = PaperBroker(500_000, slippage_bps=0, fee_bps=0)
    b.buy("ETH_JPY", 50_000, 517_777, utcnow())  # 端数の出る価格
    b.close("ETH_JPY", 517_777, utcnow())
    # 丸め誤差は ±1 円以内
    assert abs(b.cash - 500_000) <= 1


def test_buy_insufficient_cash_raises() -> None:
    b = PaperBroker(10_000, slippage_bps=0, fee_bps=15)
    with pytest.raises(BrokerError):
        b.buy("BTC_JPY", 10_000, 10_000_000, utcnow())  # 手数料分が不足


def test_close_without_position_raises() -> None:
    b = PaperBroker(10_000)
    with pytest.raises(BrokerError):
        b.close("BTC_JPY", 10_000_000, utcnow())


def test_buy_invalid_inputs_raise() -> None:
    b = PaperBroker(100_000)
    with pytest.raises(BrokerError):
        b.buy("BTC_JPY", 0, 10_000_000, utcnow())
    with pytest.raises(BrokerError):
        b.buy("BTC_JPY", 10_000, 0, utcnow())


def test_averaging_up_recomputes_avg_cost() -> None:
    b = PaperBroker(1_000_000, slippage_bps=0, fee_bps=0)
    b.buy("BTC_JPY", 100_000, 10_000_000, utcnow())
    b.buy("BTC_JPY", 100_000, 20_000_000, utcnow())
    pos = b.positions()["BTC_JPY"]
    # 平均取得単価は 2 回の取得の加重平均(1000万〜2000万の間)
    assert Decimal(10_000_000) < pos.avg_cost < Decimal(20_000_000)


def test_exposure_and_equity() -> None:
    b = PaperBroker(1_000_000, slippage_bps=0, fee_bps=0)
    fill = b.buy("BTC_JPY", 200_000, 10_000_000, utcnow())
    prices: dict[Symbol, int] = {"BTC_JPY": 10_000_000, "ETH_JPY": 500_000}
    assert abs(b.exposure(prices) - fill.notional) <= 1
    assert abs(b.equity(prices) - 1_000_000) <= 1  # 買った直後の評価額は開始資金とほぼ同じ


def test_exposure_falls_back_to_avg_cost_when_price_missing() -> None:
    """価格未取得の銘柄を 0 円評価しない(総エクスポージャの過小評価防止)。"""
    b = PaperBroker(1_000_000, slippage_bps=0, fee_bps=0)
    b.restore(
        800_000,
        [Position(symbol="BTC_JPY", qty=Decimal("0.02"), avg_cost=Decimal(10_000_000))],
    )
    empty: dict[Symbol, int] = {}
    assert b.exposure(empty) == 200_000  # 取得単価で保守的に評価
    assert b.exposure({"BTC_JPY": 0}) == 200_000  # 0 円価格も欠損扱い


def test_restore_state() -> None:
    b = PaperBroker(1)
    b.restore(
        777_000,
        [Position(symbol="ETH_JPY", qty=Decimal("0.5"), avg_cost=Decimal(500_000))],
    )
    assert b.cash == 777_000
    assert b.positions()["ETH_JPY"].qty == Decimal("0.5")


def test_unrealized_pnl_int_rounding() -> None:
    pos = Position(symbol="BTC_JPY", qty=Decimal("0.00333333"), avg_cost=Decimal("9999999.5"))
    pnl = pos.unrealized_pnl(10_000_000)
    assert isinstance(pnl, int)
