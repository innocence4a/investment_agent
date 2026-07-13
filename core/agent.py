"""トレーダーエージェント(F-2): LLM 判断サイクル。

- 出力はあくまで「提案」。発注可否はリスクゲート(risk.py)がコードで決める
- Anthropic API 呼び出しは タイムアウト・リトライ・月次コスト上限 を必ず通す(N-3)
- 全 API 応答は Store.add_api_log に記録する(安全ルール 5)
- テスト・開発用に API を使わない MockTraderAgent を同一インターフェースで提供
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from core.config import Settings
from core.indicators import macd, rsi, sma
from core.market import MarketState
from core.models import SYMBOLS, Position, Symbol, Timeframe, TraderDecision, utcnow
from core.store import Store

logger = logging.getLogger(__name__)

AGENT_NAME = "トレーダー"

# 月次コスト概算用の単価(USD / 1M トークン)。未知のモデルは保守的に高めに見積もる
MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_PRICING = (10.0, 50.0)

SYSTEM_PROMPT = """\
あなたは暗号資産(BTC/ETH、JPY 建て)のペーパートレードを行うトレーダーエージェントです。

役割:
- 与えられた市場データ・テクニカル指標・ポジション・直近成績をもとに、次のアクションを 1 つ提案する
- アクション: "buy"(新規ロング)、"close"(保有ポジションの全量決済)、"hold"(見送り)
- 現物想定のためショートはできない。ポジションがない銘柄に "close" は指定しない

制約(参考。最終的な可否はシステム側のリスクゲートが強制する):
- 1 取引の上限・日次損失上限・総エクスポージャ上限・取引頻度上限がある
- エッジ(優位性)のない取引はしない。迷ったら "hold"

出力: 次の JSON のみを返すこと。
- action: "buy" | "close" | "hold"
- symbol: "BTC_JPY" | "ETH_JPY"
- notional_jpy: buy 時の投入額(円、整数)。buy 以外は 0
- confidence: 確信度 0-100 の整数
- reason: 判断根拠を日本語で 1〜3 文。データ(RSI・MA・価格水準等)に言及して具体的に書くこと
"""

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["buy", "close", "hold"]},
        "symbol": {"type": "string", "enum": ["BTC_JPY", "ETH_JPY"]},
        "notional_jpy": {"type": "integer"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["action", "symbol", "notional_jpy", "confidence", "reason"],
    "additionalProperties": False,
}


def build_market_context(
    market: MarketState,
    positions: dict[Symbol, Position],
    *,
    cash: int,
    daily_pnl: int,
    trades_today: int,
    max_trade_notional: int,
    exclude: set[Symbol] | None = None,
) -> dict[str, Any]:
    """LLM に渡す市場スナップショットを組み立てる(JSON 化可能な dict)。

    exclude: フィード途絶中などで判断材料に含めない銘柄。
    """
    ctx: dict[str, Any] = {
        "cash_jpy": cash,
        "daily_realized_pnl_jpy": daily_pnl,
        "trades_today": trades_today,
        "max_trade_notional_jpy": max_trade_notional,
        "symbols": {},
    }
    for sym in SYMBOLS:
        if exclude and sym in exclude:
            continue
        closes = market.closes(sym, Timeframe.M5)
        if not closes:
            continue
        r = rsi(closes)
        _, _, m_hist = macd(closes)
        ma7 = sma(closes, 7)
        ma25 = sma(closes, 25)
        pos = positions.get(sym)
        candles_5m = market.candles[sym][Timeframe.M5]
        ctx["symbols"][sym] = {
            "price_jpy": market.last_price.get(sym),
            "open_5m": candles_5m[-1].o if candles_5m else None,  # 現在足の始値
            "closes_5m_last12": [int(c) for c in closes[-12:]],
            "rsi14": round(r[-1], 1) if r and r[-1] is not None else None,
            "macd_hist": round(m_hist[-1], 1) if m_hist else None,
            "ma7": round(ma7[-1], 1) if ma7 and ma7[-1] is not None else None,
            "ma25": round(ma25[-1], 1) if ma25 and ma25[-1] is not None else None,
            "position": (
                {
                    "qty": str(pos.qty),
                    "avg_cost_jpy": int(pos.avg_cost),
                    "unrealized_pnl_jpy": pos.unrealized_pnl(market.last_price.get(sym, 0)),
                }
                if pos is not None and pos.qty > 0
                else None
            ),
        }
    return ctx


class TraderAgent(Protocol):
    """LLM 実装とモック実装で共通のインターフェース。"""

    async def decide(self, context: dict[str, Any]) -> TraderDecision: ...


class CostLimitExceeded(Exception):
    """月次 LLM コスト上限に到達(判断サイクルを停止する)。"""


class AnthropicTraderAgent:
    """Anthropic API を使う本実装。"""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._client = anthropic.AsyncAnthropic(
            timeout=settings.llm_timeout_sec,
            max_retries=settings.llm_max_retries,
        )

    def _month_key(self) -> str:
        return "llm_cost_usd:" + utcnow().strftime("%Y-%m")

    async def monthly_cost_usd(self) -> float:
        raw = await self._store.get_state(self._month_key(), "0")
        return float(raw)

    def _estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        rate_in, rate_out = MODEL_PRICING_USD_PER_MTOK.get(
            self._settings.trader_model, FALLBACK_PRICING
        )
        return input_tokens / 1e6 * rate_in + output_tokens / 1e6 * rate_out

    async def decide(self, context: dict[str, Any]) -> TraderDecision:
        cost = await self.monthly_cost_usd()
        if cost >= self._settings.llm_monthly_cost_limit_usd:
            raise CostLimitExceeded(
                f"月次 LLM コスト上限(${self._settings.llm_monthly_cost_limit_usd:.2f})に"
                f"到達しました(現在 ${cost:.2f})"
            )
        request = {
            "model": self._settings.trader_model,
            "context": context,
        }
        t0 = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=self._settings.trader_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
                messages=[
                    {
                        "role": "user",
                        "content": "現在の市場スナップショット:\n"
                        + json.dumps(context, ensure_ascii=False),
                    }
                ],
            )
        except Exception as e:
            # 失敗呼び出し(タイムアウト等)もサーバー側では課金され得るため概算計上する
            est_cost = self._estimate_cost_usd(
                len(SYSTEM_PROMPT) // 3 + len(json.dumps(context)) // 3, 512
            )
            await self._store.incr_state_float(self._month_key(), est_cost)
            await self._store.add_api_log(
                ts=utcnow(), kind="llm_decision", model=self._settings.trader_model,
                ok=False, latency_ms=int((time.monotonic() - t0) * 1000), cost_usd=est_cost,
                request=request, response=f"{type(e).__name__}: {e}",
            )
            raise
        latency_ms = int((time.monotonic() - t0) * 1000)
        call_cost = self._estimate_cost_usd(
            response.usage.input_tokens, response.usage.output_tokens
        )
        await self._store.incr_state_float(self._month_key(), call_cost)
        text = next((b.text for b in response.content if b.type == "text"), "")
        await self._store.add_api_log(
            ts=utcnow(), kind="llm_decision", model=self._settings.trader_model,
            ok=True, latency_ms=latency_ms, cost_usd=call_cost,
            request=request,
            response={
                "stop_reason": response.stop_reason,
                "text": text,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            },
        )
        # フェイルセーフ: 応答不能・途中切れ・スキーマ不一致はすべて「見送り」に倒し、
        # 必ず日本語根拠付きの判断として返す(安全ルール 5: 全判断を記録)
        if response.stop_reason == "refusal" or not text:
            return TraderDecision(
                action="hold", symbol="BTC_JPY", confidence=0,
                reason="LLM 応答を取得できなかったため、安全側に倒して見送ります。",
            )
        if response.stop_reason == "max_tokens":
            return TraderDecision(
                action="hold", symbol="BTC_JPY", confidence=0,
                reason="LLM 応答が最大トークン数で途中打ち切りとなり完全な判断を取得"
                "できなかったため、安全側に倒して見送ります。",
            )
        try:
            return TraderDecision.model_validate_json(text)
        except ValidationError:
            logger.warning("LLM 応答のパースに失敗: %.200s", text)
            return TraderDecision(
                action="hold", symbol="BTC_JPY", confidence=0,
                reason="LLM 応答を判断フォーマットとして解釈できなかったため、"
                "安全側に倒して見送ります。",
            )


class MockTraderAgent:
    """API を使わないルールベース実装(開発・検証・テスト用)。

    RSI の逆張り + 保有ポジションの含み損益で判断し、日本語の根拠を生成する。
    """

    async def decide(self, context: dict[str, Any]) -> TraderDecision:
        symbols: dict[str, Any] = context.get("symbols", {})
        max_notional = int(context.get("max_trade_notional_jpy", 50_000))
        for sym_str, s in symbols.items():
            sym: Symbol = "BTC_JPY" if sym_str == "BTC_JPY" else "ETH_JPY"
            pos = s.get("position")
            r = s.get("rsi14")
            if r is None:
                # RSI(14) が揃うまで(起動後 約75分)の検証用フォールバック:
                # 直近安値割れで打診買い、±0.2% で決済。動作確認を短時間で行うための挙動
                d = self._warmup_decide(sym, s, max_notional, int(context.get("cash_jpy", 0)))
                if d is not None:
                    return d
                continue
            if pos is not None:
                upnl = int(pos.get("unrealized_pnl_jpy", 0))
                if r >= 68:
                    return TraderDecision(
                        action="close", symbol=sym, confidence=72,
                        reason=f"RSI(14) が {r} と過熱圏に到達。伸び代よりも反落リスクが"
                        "上回ると判断し、全量を利確・決済します。",
                    )
                if upnl < 0 and r >= 55:
                    return TraderDecision(
                        action="close", symbol=sym, confidence=60,
                        reason=f"含み損 {upnl} 円のまま RSI が {r} まで戻したため、"
                        "戻り売り圧力を警戒してポジションを解消します。",
                    )
                continue
            if r <= 34:
                notional = min(max_notional, int(context.get("cash_jpy", 0)))
                if notional >= 10_000:
                    return TraderDecision(
                        action="buy", symbol=sym, notional_jpy=notional, confidence=66,
                        reason=f"5分足の RSI(14) が {r} まで低下し売られ過ぎ圏。"
                        "短期リバウンド狙いでリスク上限内の小口エントリーを提案します。",
                    )
        return TraderDecision(
            action="hold", symbol="BTC_JPY", confidence=55,
            reason="RSI・MACD ともに中立圏で優位性のあるセットアップがありません。"
            "エッジのない取引はしない方針に従い、今サイクルは見送ります。",
        )

    def _warmup_decide(
        self, sym: Symbol, s: dict[str, Any], max_notional: int, cash: int
    ) -> TraderDecision | None:
        """指標が揃う前(ウォームアップ中)の簡易判断。開発・動作確認用。"""
        price = s.get("price_jpy")
        closes = s.get("closes_5m_last12") or []
        pos = s.get("position")
        if price is None:
            return None
        if pos is not None:
            avg = int(pos.get("avg_cost_jpy", 0)) or 1
            pct = (int(price) - avg) / avg * 100
            if abs(pct) >= 0.2:
                label = "利確" if pct > 0 else "損切り"
                return TraderDecision(
                    action="close", symbol=sym, confidence=58,
                    reason=f"取得単価比 {pct:+.2f}% に到達。指標蓄積中の小口検証ルール"
                    f"(±0.2% で{label})に従い決済します。",
                )
            return None
        open_5m = s.get("open_5m")
        dip_in_candle = open_5m is not None and int(price) < int(open_5m)
        dip_vs_closes = len(closes) >= 2 and int(price) <= min(int(c) for c in closes)
        if dip_in_candle or dip_vs_closes:
            notional = min(20_000, max_notional, cash)
            if notional >= 10_000:
                return TraderDecision(
                    action="buy", symbol=sym, notional_jpy=notional, confidence=56,
                    reason="RSI(14) の算出に必要な履歴を蓄積中のため、足内の押し目での"
                    "打診買いルールで小口エントリーします(検証用の暫定ロジック)。",
                )
        return None
