"""相談役エージェント(F-21)のテスト。LLM は API モック(実 API 非依存)。"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.advisor import AnthropicAdvisor, MockAdvisor
from core.config import Settings
from core.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(str(tmp_path / "adv.db"))
    await s.open()
    yield s
    await s.close()


CONTEXT: dict[str, Any] = {
    "kpi": {"equity": 1_002_000, "pnl_total": 2_000, "pnl_today": 500,
            "wins": 3, "losses": 2, "trades": 10},
    "macro": {"vix": {"value": 18.4, "change_pct": 0.5}},
    "upcoming_events": [{"label": "米CPI(6月分)", "importance": "hi", "minutes_until": 120}],
    "restraint": {"mode": "none", "reasons": []},
    "recent_decisions": [],
}


async def test_mock_advisor_mentions_key_facts() -> None:
    text = await MockAdvisor().review(CONTEXT)
    assert "VIX" in text
    assert "米CPI" in text
    assert "人間" in text  # 適用は人間承認が前提であることを明記


class FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=800, output_tokens=400),
        )


async def test_anthropic_advisor_records_cost_and_log(store: Store) -> None:
    advisor = AnthropicAdvisor(Settings(), store)
    fake = FakeMessages("本日の地合いは中立。トレンドフォロー優位の環境です。")
    advisor._client = SimpleNamespace(messages=fake)  # type: ignore[assignment]
    text = await advisor.review(CONTEXT)
    assert "地合い" in text
    cur = await store.db.execute("SELECT kind, ok FROM api_log")
    rows = list(await cur.fetchall())
    assert rows[0]["kind"] == "llm_advisor"
    assert rows[0]["ok"] == 1
    # トレーダーと同じ月次コストカウンタに計上される
    month_cost = float(await store.get_state(advisor._month_key(), "0"))
    assert month_cost > 0


def test_advisor_hour_validation() -> None:
    # 不正な時刻設定は起動時に弾く(advisor ループの連続クラッシュ → 緊急停止連鎖の防止)
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        Settings(advisor_hour_jst=24)
    with _pytest.raises(ValidationError):
        Settings(advisor_hour_jst=-1)
    assert Settings(advisor_hour_jst=0).advisor_hour_jst == 0


async def test_anthropic_advisor_respects_cost_limit(store: Store) -> None:
    advisor = AnthropicAdvisor(Settings(llm_monthly_cost_limit_usd=0.001), store)
    await store.set_state(advisor._month_key(), "1.0")  # 既に上限超過
    fake = FakeMessages("x")
    advisor._client = SimpleNamespace(messages=fake)  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await advisor.review(CONTEXT)
    assert fake.calls == []  # API は呼ばれない
