"""関連指標(F-18)のテスト: パーサ(実 API の応答形式)とシミュレーションソース。"""

from core.macro import SimMacroSource, change_pct, parse_fng, parse_yahoo_chart
from core.models import MACRO_KEYS


def test_parse_fng() -> None:
    data = {
        "name": "Fear and Greed Index",
        "data": [{"value": "62", "value_classification": "Greed", "timestamp": "1752451200"}],
    }
    assert parse_fng(data) == 62.0


def test_parse_yahoo_chart() -> None:
    data = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 18.42,
                        "chartPreviousClose": 17.9,
                        "symbol": "^VIX",
                    },
                    "timestamp": [],
                    "indicators": {},
                }
            ],
            "error": None,
        }
    }
    price, prev = parse_yahoo_chart(data)
    assert price == 18.42
    assert prev == 17.9


def test_parse_yahoo_chart_without_prev_close() -> None:
    data = {"chart": {"result": [{"meta": {"regularMarketPrice": 100.0}}]}}
    price, prev = parse_yahoo_chart(data)
    assert (price, prev) == (100.0, None)


def test_change_pct() -> None:
    assert change_pct(110.0, 100.0) == 10.0
    assert change_pct(90.0, 100.0) == -10.0
    assert change_pct(1.0, None) is None
    assert change_pct(1.0, 0.0) is None


async def test_sim_macro_source_produces_all_tiles() -> None:
    src = SimMacroSource(seed=42)
    snap = await src.fetch()
    assert set(snap.tiles.keys()) == set(MACRO_KEYS)
    fng = snap.tiles["fear_greed"]
    assert 0.0 <= fng.value <= 100.0
    snap2 = await src.fetch()
    assert snap2.tiles["vix"].change_pct is not None  # 2 回目以降は基準比が付く
