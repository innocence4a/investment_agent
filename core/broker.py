"""ブローカー(F-4)。

PAPER / LIVE は同一インターフェース(Broker Protocol)で実装し、切替は設定 1 箇所。
Phase 1 は PaperBroker のみ。LIVE 実装(Phase 2)は発注者の明示的承認なしに追加しない。

会計ルール:
- 現金は JPY int。数量は Decimal(8 桁切り捨て)
- 買い: 約定価格 = 実勢 × (1 + slippage)。取得原価に手数料を含める
- 売り: 約定価格 = 実勢 × (1 - slippage)。実現損益 = 受取額 − 手数料 − 取得原価
- Phase 1 は現物想定のロングのみ(ショートなし)
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from core.models import Fill, Position, Symbol, notional_jpy, quantize_qty


class BrokerError(Exception):
    """発注不能(残高不足・数量不正など)。"""


class Broker(Protocol):
    """ペーパー/実弾で共通の発注インターフェース。"""

    @property
    def cash(self) -> int: ...

    def positions(self) -> dict[Symbol, Position]: ...

    def buy(
        self, symbol: Symbol, notional: int, market_price: int, ts: datetime,
        decision_id: int | None = None,
    ) -> Fill: ...

    def close(
        self, symbol: Symbol, market_price: int, ts: datetime,
        decision_id: int | None = None,
    ) -> Fill: ...


class PaperBroker:
    """実勢価格+想定スリッページで仮想約定するペーパーブローカー。"""

    def __init__(self, cash: int, *, slippage_bps: int = 5, fee_bps: int = 15) -> None:
        self._cash = cash
        self._slippage_bps = slippage_bps
        self._fee_bps = fee_bps
        self._positions: dict[Symbol, Position] = {}

    # ── 状態 ──
    @property
    def cash(self) -> int:
        return self._cash

    def positions(self) -> dict[Symbol, Position]:
        return {s: p for s, p in self._positions.items() if p.qty > 0}

    def restore(self, cash: int, positions: list[Position]) -> None:
        """再起動時に DB から状態を復元する。"""
        self._cash = cash
        self._positions = {p.symbol: p for p in positions if p.qty > 0}

    def exposure(self, prices: dict[Symbol, int]) -> int:
        """全ポジションの評価額合計(JPY)。

        価格が未取得の銘柄は取得単価で評価する(0 円評価にすると総エクスポージャを
        過小評価し、リスクゲートの上限判定が甘くなるため)。
        """
        total = 0
        for s, p in self.positions().items():
            price = prices.get(s)
            if price is None or price <= 0:
                price = int(p.avg_cost.quantize(Decimal("1"), ROUND_HALF_UP))
            total += notional_jpy(p.qty, price)
        return total

    def equity(self, prices: dict[Symbol, int]) -> int:
        return self._cash + self.exposure(prices)

    # ── 約定 ──
    def _fill_price(self, market_price: int, side: str) -> int:
        slip = Decimal(market_price) * self._slippage_bps / 10_000
        raw = Decimal(market_price) + (slip if side == "buy" else -slip)
        return int(raw.quantize(Decimal("1"), ROUND_HALF_UP))

    def _fee(self, notional: int) -> int:
        return int(
            (Decimal(notional) * self._fee_bps / 10_000).quantize(Decimal("1"), ROUND_HALF_UP)
        )

    def buy(
        self, symbol: Symbol, notional: int, market_price: int, ts: datetime,
        decision_id: int | None = None,
    ) -> Fill:
        if notional <= 0 or market_price <= 0:
            raise BrokerError("発注額・価格が不正です")
        price = self._fill_price(market_price, "buy")
        qty = quantize_qty(Decimal(notional) / Decimal(price))
        if qty <= 0:
            raise BrokerError("数量が最小単位未満です")
        cost = notional_jpy(qty, price)
        fee = self._fee(cost)
        if cost + fee > self._cash:
            raise BrokerError(f"残高不足: 必要 ¥{cost + fee:,} / 現金 ¥{self._cash:,}")
        self._cash -= cost + fee
        pos = self._positions.get(symbol)
        if pos is None or pos.qty <= 0:
            avg = (Decimal(cost + fee) / qty) if qty else Decimal(0)
            self._positions[symbol] = Position(symbol=symbol, qty=qty, avg_cost=avg)
        else:
            total_cost = pos.avg_cost * pos.qty + Decimal(cost + fee)
            new_qty = pos.qty + qty
            self._positions[symbol] = Position(
                symbol=symbol, qty=new_qty, avg_cost=total_cost / new_qty
            )
        return Fill(
            ts=ts, symbol=symbol, side="buy", qty=qty, price=price,
            notional=cost, fee=fee, decision_id=decision_id,
        )

    def close(
        self, symbol: Symbol, market_price: int, ts: datetime,
        decision_id: int | None = None,
    ) -> Fill:
        """保有ポジションを全量成行で決済する。"""
        pos = self._positions.get(symbol)
        if pos is None or pos.qty <= 0:
            raise BrokerError(f"{symbol} のポジションがありません")
        price = self._fill_price(market_price, "sell")
        qty = pos.qty
        proceeds = notional_jpy(qty, price)
        fee = self._fee(proceeds)
        cost_basis = int((pos.avg_cost * qty).quantize(Decimal("1"), ROUND_HALF_UP))
        realized = proceeds - fee - cost_basis
        self._cash += proceeds - fee
        self._positions[symbol] = Position(symbol=symbol, qty=Decimal(0), avg_cost=Decimal(0))
        return Fill(
            ts=ts, symbol=symbol, side="sell", qty=qty, price=price,
            notional=proceeds, fee=fee, realized_pnl=realized, decision_id=decision_id,
        )
