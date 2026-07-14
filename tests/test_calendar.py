"""経済指標カレンダー(F-19)のテスト: ルール生成・静的読込・抑制ウィンドウ境界。"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.calendar import EconomicCalendar, first_friday, load_static_events, nfp_events
from core.models import EconomicEvent


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_first_friday() -> None:
    assert first_friday(2026, 7) == ts("2026-07-03 00:00:00")  # 2026-07-03 は金曜
    assert first_friday(2026, 8) == ts("2026-08-07 00:00:00")
    assert first_friday(2026, 11) == ts("2026-11-06 00:00:00")


def test_nfp_rule_generation_with_dst() -> None:
    events = nfp_events(ts("2026-07-01 00:00:00"), months=6)
    assert len(events) == 6
    # 夏時間(7月)は 12:30 UTC、冬時間(11月)は 13:30 UTC
    jul = next(e for e in events if e.id == "nfp-2026-07")
    nov = next(e for e in events if e.id == "nfp-2026-11")
    assert (jul.ts.hour, jul.ts.minute) == (12, 30)
    assert (nov.ts.hour, nov.ts.minute) == (13, 30)
    assert jul.importance == "hi"
    assert jul.window_before_min == 30


def test_window_active_boundaries() -> None:
    e = EconomicEvent(
        id="x", label="テスト", importance="hi",
        ts=ts("2026-07-14 12:30:00"), window_before_min=30, window_after_min=30,
    )
    assert e.window_active(ts("2026-07-14 12:00:00")) is True  # 開始ちょうど
    assert e.window_active(ts("2026-07-14 11:59:59")) is False
    assert e.window_active(ts("2026-07-14 13:00:00")) is True  # 終了ちょうど
    assert e.window_active(ts("2026-07-14 13:00:01")) is False


def test_load_static_events_skips_broken_entries(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({
        "events": [
            {"id": "ok", "label": "米CPI", "importance": "hi",
             "ts": "2026-08-12T12:30:00+00:00", "window_before_min": 30,
             "window_after_min": 30},
            {"id": "broken", "label": "壊れた項目"},  # ts なし → 読み飛ばし
        ]
    }), encoding="utf-8")
    events = load_static_events(path)
    assert [e.id for e in events] == ["ok"]


def test_calendar_merges_static_and_rule_with_override(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    # ルール生成と同じ id を静的定義で上書きできる(手動修正を優先)
    path.write_text(json.dumps({
        "events": [
            {"id": "nfp-2026-07", "label": "米雇用統計(手動修正)", "importance": "hi",
             "ts": "2026-07-03T12:00:00+00:00", "window_before_min": 45,
             "window_after_min": 45},
        ]
    }), encoding="utf-8")
    cal = EconomicCalendar(path, now=ts("2026-07-01 00:00:00"))
    nfp_jul = [e for e in cal.events if e.id == "nfp-2026-07"]
    assert len(nfp_jul) == 1
    assert nfp_jul[0].window_before_min == 45  # 静的定義が勝つ
    # ルール生成分(翌月以降の NFP)も含まれている
    assert any(e.id == "nfp-2026-08" for e in cal.events)


def test_upcoming_and_active(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    now = ts("2026-07-14 12:45:00")
    path.write_text(json.dumps({
        "events": [
            {"id": "past", "label": "過去", "importance": "hi",
             "ts": "2026-07-10T12:30:00+00:00", "window_before_min": 30,
             "window_after_min": 30},
            {"id": "active", "label": "発表直後", "importance": "hi",
             "ts": "2026-07-14T12:30:00+00:00", "window_before_min": 30,
             "window_after_min": 30},
            {"id": "future", "label": "来週", "importance": "mid",
             "ts": "2026-07-21T12:30:00+00:00", "window_before_min": 30,
             "window_after_min": 30},
        ]
    }), encoding="utf-8")
    cal = EconomicCalendar(path, now=now)
    upcoming_ids = [e.id for e in cal.upcoming(now)]
    assert "past" not in upcoming_ids
    assert upcoming_ids[0] == "active"  # ウィンドウ未終了は「今後」に含める
    assert [e.id for e in cal.active_events(now)] == ["active"]
