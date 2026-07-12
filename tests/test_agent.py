"""トレーダーエージェント(F-2)のテスト。

品質ゲート要件: LLM 呼び出しは API をモックしてテスト(実 API に依存しない)。
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agent import AnthropicTraderAgent, CostLimitExceeded, MockTraderAgent
from core.config import Settings
from core.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(str(tmp_path / "agent.db"))
    await s.open()
    yield s
    await s.close()


def make_settings(**kw: Any) -> Settings:
    return Settings(llm_monthly_cost_limit_usd=kw.pop("limit", 50.0), **kw)


class FakeMessages:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.text = text
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def patch_client(agent: AnthropicTraderAgent, fake: FakeMessages) -> None:
    agent._client = SimpleNamespace(messages=fake)  # type: ignore[assignment]


CONTEXT: dict[str, Any] = {"cash_jpy": 1_000_000, "symbols": {}, "max_trade_notional_jpy": 50_000}


async def test_decide_parses_structured_output(store: Store) -> None:
    agent = AnthropicTraderAgent(make_settings(), store)
    decision_json = json.dumps({
        "action": "buy", "symbol": "BTC_JPY", "notional_jpy": 30_000,
        "confidence": 70, "reason": "RSI が売られ過ぎ圏のため打診買い。",
    })
    patch_client(agent, FakeMessages(decision_json))
    d = await agent.decide(CONTEXT)
    assert d.action == "buy"
    assert d.notional_jpy == 30_000
    assert "RSI" in d.reason


async def test_decide_records_api_log_and_cost(store: Store) -> None:
    agent = AnthropicTraderAgent(make_settings(), store)
    patch_client(agent, FakeMessages(json.dumps({
        "action": "hold", "symbol": "BTC_JPY", "notional_jpy": 0,
        "confidence": 50, "reason": "見送り。",
    })))
    await agent.decide(CONTEXT)
    # コストカウンタが加算されている(sonnet: 1000in+200out > 0)
    assert await agent.monthly_cost_usd() > 0
    cur = await store.db.execute("SELECT kind, ok, model FROM api_log")
    rows = list(await cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["kind"] == "llm_decision"
    assert rows[0]["ok"] == 1


async def test_cost_limit_stops_decisions(store: Store) -> None:
    agent = AnthropicTraderAgent(make_settings(limit=0.001), store)
    fake = FakeMessages(json.dumps({
        "action": "hold", "symbol": "BTC_JPY", "notional_jpy": 0,
        "confidence": 50, "reason": "見送り。",
    }))
    patch_client(agent, fake)
    await agent.decide(CONTEXT)  # 1 回目でカウンタが上限超え
    with pytest.raises(CostLimitExceeded):
        await agent.decide(CONTEXT)
    assert len(fake.calls) == 1  # 2 回目は API を呼ばない


async def test_refusal_falls_back_to_hold(store: Store) -> None:
    agent = AnthropicTraderAgent(make_settings(), store)
    patch_client(agent, FakeMessages("", stop_reason="refusal"))
    d = await agent.decide(CONTEXT)
    assert d.action == "hold"
    assert d.reason  # 日本語根拠が必ず付く


async def test_api_error_is_logged_and_reraised(store: Store) -> None:
    agent = AnthropicTraderAgent(make_settings(), store)

    class Boom:
        async def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("api down")

    agent._client = SimpleNamespace(messages=Boom())  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await agent.decide(CONTEXT)
    cur = await store.db.execute("SELECT ok FROM api_log")
    rows = list(await cur.fetchall())
    assert rows[0]["ok"] == 0


# ── MockTraderAgent(ルールベース) ──────────────────────


async def test_mock_buys_on_oversold_rsi() -> None:
    ctx = {
        "cash_jpy": 1_000_000, "max_trade_notional_jpy": 50_000,
        "symbols": {"BTC_JPY": {"rsi14": 28.0, "position": None}},
    }
    d = await MockTraderAgent().decide(ctx)
    assert d.action == "buy"
    assert d.notional_jpy == 50_000  # 上限内
    assert d.reason


async def test_mock_closes_on_overbought_rsi() -> None:
    ctx = {
        "cash_jpy": 1_000_000, "max_trade_notional_jpy": 50_000,
        "symbols": {"BTC_JPY": {
            "rsi14": 71.0,
            "position": {"qty": "0.001", "avg_cost_jpy": 1, "unrealized_pnl_jpy": 100},
        }},
    }
    d = await MockTraderAgent().decide(ctx)
    assert d.action == "close"


async def test_mock_warmup_buys_at_recent_low_without_rsi() -> None:
    # RSI(14) が揃うまでの検証用フォールバック: 直近安値タッチで小口打診買い
    ctx = {
        "cash_jpy": 1_000_000, "max_trade_notional_jpy": 50_000,
        "symbols": {"BTC_JPY": {
            "rsi14": None, "price_jpy": 990, "closes_5m_last12": [1000, 995], "position": None,
        }},
    }
    d = await MockTraderAgent().decide(ctx)
    assert d.action == "buy"
    assert d.notional_jpy <= 20_000


async def test_mock_warmup_closes_at_small_move() -> None:
    ctx = {
        "cash_jpy": 1_000_000, "max_trade_notional_jpy": 50_000,
        "symbols": {"BTC_JPY": {
            "rsi14": None, "price_jpy": 1_003, "closes_5m_last12": [1000],
            "position": {"qty": "1", "avg_cost_jpy": 1000, "unrealized_pnl_jpy": 3},
        }},
    }
    d = await MockTraderAgent().decide(ctx)  # +0.3% >= 0.2% → 決済
    assert d.action == "close"


async def test_mock_holds_in_neutral_market() -> None:
    ctx = {
        "cash_jpy": 1_000_000, "max_trade_notional_jpy": 50_000,
        "symbols": {"BTC_JPY": {"rsi14": 50.0, "position": None}},
    }
    d = await MockTraderAgent().decide(ctx)
    assert d.action == "hold"
    assert d.reason
