"""相談役エージェント(F-21)。

日次でマクロ環境・成績・判断ログを俯瞰してレビューし、所感・改善提案を
思考ログ(と Slack のモーニングレポート)に出力する。

安全設計(CLAUDE.md): 相談役は助言のみ。発注経路には一切接続せず、
提案の適用(パラメータ変更等)は人間承認で行う。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

import anthropic

from core.config import Settings
from core.models import utcnow
from core.store import Store

logger = logging.getLogger(__name__)

AGENT_NAME = "相談役"

SYSTEM_PROMPT = """\
あなたは暗号資産ペーパートレードシステムの「相談役」エージェントです。

役割:
- 与えられた成績・判断ログの傾向・関連指標(マクロ環境)・今後の経済イベントを俯瞰し、
  日次レビューを日本語で出力する
- 内容: ①昨日〜直近の成績と判断の質への所感、②現在の地合いの解釈(関連指標から)、
  ③今後のイベントへの注意点、④改善提案(あれば)
- あなたには売買の権限はない。提案はすべて「人間の承認を経て適用される助言」として書く
- 具体的な数値・指標に言及し、300〜500 文字程度の簡潔な文章にまとめる(見出し・箇条書き不要)
"""


class AdvisorAgent(Protocol):
    async def review(self, context: dict[str, Any]) -> str: ...


class AnthropicAdvisor:
    """Anthropic API を使う本実装。トレーダーと同じ月次コスト上限カウンタを共有する。"""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._client = anthropic.AsyncAnthropic(
            timeout=settings.llm_timeout_sec,
            max_retries=settings.llm_max_retries,
        )

    def _month_key(self) -> str:
        return "llm_cost_usd:" + utcnow().strftime("%Y-%m")

    async def review(self, context: dict[str, Any]) -> str:
        cost = float(await self._store.get_state(self._month_key(), "0"))
        if cost >= self._settings.llm_monthly_cost_limit_usd:
            raise RuntimeError(
                f"月次 LLM コスト上限(${self._settings.llm_monthly_cost_limit_usd:.2f})に"
                "到達しているため、相談役レビューをスキップします"
            )
        t0 = time.monotonic()
        request = {"model": self._settings.advisor_model, "context": context}
        try:
            response = await self._client.messages.create(
                model=self._settings.advisor_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": "日次レビューの材料:\n"
                        + json.dumps(context, ensure_ascii=False),
                    }
                ],
            )
        except Exception as e:
            await self._store.add_api_log(
                ts=utcnow(), kind="llm_advisor", model=self._settings.advisor_model,
                ok=False, latency_ms=int((time.monotonic() - t0) * 1000), cost_usd=None,
                request=request, response=f"{type(e).__name__}: {e}",
            )
            raise
        # コスト概算(単価はトレーダーと同じ表を利用)
        from core.agent import FALLBACK_PRICING, MODEL_PRICING_USD_PER_MTOK

        rate_in, rate_out = MODEL_PRICING_USD_PER_MTOK.get(
            self._settings.advisor_model, FALLBACK_PRICING
        )
        call_cost = (
            response.usage.input_tokens / 1e6 * rate_in
            + response.usage.output_tokens / 1e6 * rate_out
        )
        await self._store.incr_state_float(self._month_key(), call_cost)
        text = next((b.text for b in response.content if b.type == "text"), "")
        await self._store.add_api_log(
            ts=utcnow(), kind="llm_advisor", model=self._settings.advisor_model,
            ok=True, latency_ms=int((time.monotonic() - t0) * 1000), cost_usd=call_cost,
            request=request, response={"stop_reason": response.stop_reason, "text": text},
        )
        if not text:
            raise RuntimeError("相談役レビューの応答が空でした")
        return text


class MockAdvisor:
    """API を使わないテンプレート実装(開発・検証・テスト用)。"""

    async def review(self, context: dict[str, Any]) -> str:
        kpi = context.get("kpi", {})
        macro = context.get("macro", {})
        events = context.get("upcoming_events", [])
        vix = macro.get("vix", {}).get("value")
        parts = [
            f"直近の成績は累計 {kpi.get('pnl_total', 0):+,} 円"
            f"(勝ち {kpi.get('wins', 0)} / 負け {kpi.get('losses', 0)})。",
        ]
        if vix is not None:
            mood = (
                "警戒水準で、守りを優先すべき地合い"
                if vix >= 25
                else "落ち着いており、トレンドフォロー優位の環境"
            )
            parts.append(f"VIX は {vix:.1f} と{mood}と見ています。")
        if events:
            parts.append(
                f"直近では「{events[0].get('label')}」を控えており、発表前後は"
                "カレンダー連動の抑制ウィンドウに従って無理をしない方針が適切です。"
            )
        parts.append(
            "見送り判断の質は概ね良好です。パラメータ変更の提案は現時点ではありません"
            "(適用はいずれも人間承認が前提です)。"
        )
        return "".join(parts)
