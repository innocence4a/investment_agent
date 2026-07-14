"""共有データモデル。

金額の扱い(CLAUDE.md 準拠):
- JPY は int(円)。float を金額に使わない
- 暗号資産数量は Decimal(8 桁量子化)
- datetime は必ず UTC aware。表示時のみ JST に変換する
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

QTY_EXP = Decimal("0.00000001")  # 数量は 8 桁(satoshi 精度)

Side = Literal["buy", "sell"]
Symbol = Literal["BTC_JPY", "ETH_JPY"]
SYMBOLS: tuple[Symbol, ...] = ("BTC_JPY", "ETH_JPY")


def utcnow() -> datetime:
    return datetime.now(UTC)


def quantize_qty(qty: Decimal) -> Decimal:
    """数量を 8 桁に切り捨て量子化する(過大注文を防ぐため常に切り捨て)。"""
    return qty.quantize(QTY_EXP, rounding=ROUND_DOWN)


def notional_jpy(qty: Decimal, price: int) -> int:
    """数量 × 価格 の JPY 想定元本(四捨五入で int 化)。"""
    return int((qty * price).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class Timeframe(enum.StrEnum):
    M5 = "5m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    MO1 = "1M"


TF_MINUTES: dict[Timeframe, int] = {
    Timeframe.M5: 5,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
    Timeframe.W1: 10080,
    Timeframe.MO1: 43200,  # 月足はカレンダー月で区切る(この値はラベル用の目安)
}


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Ticker(FrozenModel):
    """価格フィードの 1 ティック。"""

    symbol: Symbol
    price: int  # JPY
    ts: datetime

    @field_serializer("ts")
    def _ser_ts(self, v: datetime) -> str:
        return v.isoformat()


class Candle(BaseModel):
    """ローソク足(open_ts はタイムフレーム境界に整列した UTC)。"""

    symbol: Symbol
    tf: Timeframe
    open_ts: datetime
    o: int
    h: int
    l: int  # noqa: E741
    c: int

    @field_serializer("open_ts")
    def _ser_open_ts(self, v: datetime) -> str:
        return v.isoformat()


class TraderDecision(FrozenModel):
    """トレーダーエージェント(LLM)の 1 回の判断出力。

    これは「提案」であり、発注はリスクゲート(risk.py)通過後にのみ行われる。
    """

    action: Literal["buy", "close", "hold"]
    symbol: Symbol
    notional_jpy: int = 0  # buy 時の希望投入額(JPY)
    confidence: int = 0  # 0-100
    reason: str  # 日本語の判断根拠(必須)


class GateResult(FrozenModel):
    """リスクゲートの検査結果。"""

    allowed: bool
    code: str  # "PASS" / "HALTED" / "PER_TRADE_LIMIT" / ...
    reason: str  # 日本語の説明(画面・記録用)


class Fill(BaseModel):
    """約定(ペーパーブローカーの仮想約定)。"""

    id: int = 0
    ts: datetime
    symbol: Symbol
    side: Side
    qty: Decimal
    price: int  # 約定価格(スリッページ込み・JPY)
    notional: int  # JPY
    fee: int  # JPY
    realized_pnl: int | None = None  # 決済(sell)時のみ。手数料控除後
    decision_id: int | None = None

    @field_serializer("ts")
    def _ser_ts(self, v: datetime) -> str:
        return v.isoformat()

    @field_serializer("qty")
    def _ser_qty(self, v: Decimal) -> str:
        return str(v)


class Position(BaseModel):
    """保有ポジション(Phase 1 は現物想定のロングのみ)。"""

    symbol: Symbol
    qty: Decimal
    avg_cost: Decimal  # 1 単位あたり取得単価(手数料込み・JPY)。計算精度のため Decimal

    @field_serializer("qty")
    def _ser_qty(self, v: Decimal) -> str:
        return str(v)

    @field_serializer("avg_cost")
    def _ser_avg_cost(self, v: Decimal) -> int:
        return int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def unrealized_pnl(self, price: int) -> int:
        return int(((price - self.avg_cost) * self.qty).quantize(Decimal("1"), ROUND_HALF_UP))


ThoughtKind = Literal["buy", "sell", "close", "skip", "risk", "advice", "system"]


class Thought(BaseModel):
    """思考ログの 1 カード(F-11)。全判断・システムイベントを記録する。"""

    id: int = 0
    ts: datetime
    agent: str  # "トレーダー" / "システム" 等
    kind: ThoughtKind
    text: str  # 日本語の根拠・説明
    symbol: Symbol | None = None
    confidence: int | None = None
    gate: str | None = None  # リスクゲート結果の表示文言(例: "通過" / "発動(日次損失上限)")

    @field_serializer("ts")
    def _ser_ts(self, v: datetime) -> str:
        return v.isoformat()


class Kpi(BaseModel):
    """KPI ヘッダ(F-8)用スナップショット。金額はすべて JPY int。"""

    equity: int
    cash: int
    start_capital: int
    pnl_total: int
    pnl_today: int
    wins: int
    losses: int
    trades: int
    started_at: datetime
    halted: bool
    monthly_llm_cost_usd: float  # LLM コスト(参考表示。金銭計算には使わない)

    @field_serializer("started_at")
    def _ser_started(self, v: datetime) -> str:
        return v.isoformat()


class EquityPoint(BaseModel):
    ts: datetime
    equity: int

    @field_serializer("ts")
    def _ser_ts(self, v: datetime) -> str:
        return v.isoformat()


# ── Phase 1.5: 関連指標(F-18) ──────────────────────

MacroKey = Literal["fear_greed", "vix", "gold_usd", "sp500", "dxy", "us10y"]
MACRO_KEYS: tuple[MacroKey, ...] = ("fear_greed", "vix", "gold_usd", "sp500", "dxy", "us10y")


class MacroTile(BaseModel):
    """関連指標 1 件。表示・判断材料用であり金銭計算には使わない(float 可)。"""

    key: MacroKey
    value: float
    change_pct: float | None = None  # 基準値(前回終値等)からの変化率
    ts: datetime

    @field_serializer("ts")
    def _ser_ts(self, v: datetime) -> str:
        return v.isoformat()


class MacroSnapshot(BaseModel):
    """関連指標の最新スナップショット。取得失敗した指標は含まれない。"""

    tiles: dict[MacroKey, MacroTile] = {}
    fetched_at: datetime | None = None

    @field_serializer("fetched_at")
    def _ser_fetched(self, v: datetime | None) -> str | None:
        return v.isoformat() if v else None


# ── Phase 1.5: 経済指標カレンダー(F-19) ────────────

Importance = Literal["hi", "mid", "lo"]


class EconomicEvent(BaseModel):
    """経済指標・イベントの予定 1 件(時刻は UTC)。"""

    id: str
    label: str  # 例: "米雇用統計(NFP)"
    importance: Importance
    ts: datetime
    window_before_min: int = 0  # 発表前の取引抑制ウィンドウ(分)
    window_after_min: int = 0  # 発表後の取引抑制ウィンドウ(分)
    note: str = ""

    @field_serializer("ts")
    def _ser_ts(self, v: datetime) -> str:
        return v.isoformat()

    def window_active(self, now: datetime) -> bool:
        """now が取引抑制ウィンドウ内かどうか。境界は「ちょうど」も含む。"""
        start = self.ts - timedelta(minutes=self.window_before_min)
        end = self.ts + timedelta(minutes=self.window_after_min)
        return start <= now <= end


# ── Phase 1.5: リスク管理エージェントの抑制状態(F-20) ──


class RestraintMode(enum.StrEnum):
    """取引抑制モード。コードのリスクゲート層が強制する状態(LLM は直接触れない)。"""

    NONE = "none"  # 通常
    SIZE_HALF = "size_half"  # 新規エントリーのサイズを半分に制限
    NO_ENTRY = "no_entry"  # 新規エントリー停止(決済・損切り監視は継続)


class RestraintState(BaseModel):
    mode: RestraintMode = RestraintMode.NONE
    reasons: list[str] = []  # 日本語の根拠(思考ログ・画面表示用)
