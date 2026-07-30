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
    # 8:30 ET = 夏時間 12:30 UTC / 冬時間 13:30 UTC(zoneinfo で正確に解決)
    jul = next(e for e in events if e.id == "nfp-2026-07")
    nov = next(e for e in events if e.id == "nfp-2026-11")
    assert (jul.ts.hour, jul.ts.minute) == (12, 30)  # 7/3 は夏時間
    # 2026-11-06: DST は 11/1(第1日曜)に終了済み → 冬時間 13:30 UTC
    assert (nov.ts.hour, nov.ts.minute) == (13, 30)
    assert jul.importance == "hi"
    assert jul.window_before_min == 30


def test_nfp_november_during_dst_edge_year() -> None:
    # 2030-11-01 は金曜かつ DST 終了(11/3 第1日曜)前 → 夏時間 12:30 UTC。
    # 月単位の近似だと 13:30 になり、±30 分ウィンドウが実発表(12:30)を外れる回帰ケース
    events = nfp_events(ts("2030-11-01 00:00:00"), months=1)
    nov = events[0]
    assert nov.ts == ts("2030-11-01 12:30:00")


def test_default_calendar_ships_with_hi_impact_static_events() -> None:
    """同梱カレンダーの存在保証(.gitignore 誤マッチでの欠落を CI で検知する)。"""
    from core.calendar import DEFAULT_CALENDAR_PATH

    assert DEFAULT_CALENDAR_PATH.exists(), (
        f"{DEFAULT_CALENDAR_PATH} がリポジトリに存在しません(.gitignore を確認)"
    )
    static = load_static_events(DEFAULT_CALENDAR_PATH)
    assert any(e.id.startswith("fomc-") and e.importance == "hi" for e in static)
    assert any(e.id.startswith("cpi-") for e in static)
    cal = EconomicCalendar()
    assert cal.static_missing is False


def test_naive_ts_is_rejected(tmp_path: Path) -> None:
    """tzinfo の無い ts は読み飛ばす(ローカル TZ 解釈でサイレントに最大 9 時間ずれるため)。"""
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({
        "events": [
            {"id": "naive", "label": "naive 時刻", "importance": "hi",
             "ts": "2026-08-12T12:30:00", "window_before_min": 30, "window_after_min": 30},
            {"id": "aware", "label": "正しい時刻", "importance": "hi",
             "ts": "2026-08-12T12:30:00+00:00", "window_before_min": 30,
             "window_after_min": 30},
        ]
    }), encoding="utf-8")
    events = load_static_events(path)
    assert [e.id for e in events] == ["aware"]


def test_refresh_reflects_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"events": []}), encoding="utf-8")
    cal = EconomicCalendar(path, now=ts("2026-07-01 00:00:00"))
    assert not any(e.id == "added" for e in cal.events)
    path.write_text(json.dumps({
        "events": [{"id": "added", "label": "追加イベント", "importance": "hi",
                    "ts": "2026-08-12T12:30:00+00:00", "window_before_min": 30,
                    "window_after_min": 30}]
    }), encoding="utf-8")
    cal.refresh(now=ts("2026-07-01 00:00:00"))
    assert any(e.id == "added" for e in cal.events)


def test_missing_default_file_sets_static_missing(tmp_path: Path) -> None:
    cal = EconomicCalendar(tmp_path / "not-exists.json", now=ts("2026-07-01 00:00:00"))
    assert cal.static_missing is True
    assert any(e.id.startswith("nfp-") for e in cal.events)  # ルール生成は生きている


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
