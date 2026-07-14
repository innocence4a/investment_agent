"""経済指標カレンダー(F-19)。

- 発表スケジュールを「事前管理」する: 静的ファイル(core/data/economic_calendar.json、
  IA_CALENDAR_PATH で差し替え可)+ ルール生成(米雇用統計 = 毎月第 1 金曜)
- ダッシュボードに「今後の予定+カウントダウン」を表示し、発表前後の取引抑制ウィンドウは
  リスク管理エージェント(core/risk_agent.py)経由でリスクゲートに反映される
- 時刻はすべて UTC。表示時に JST 変換
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.models import EconomicEvent, utcnow

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_PATH = Path(__file__).parent / "data" / "economic_calendar.json"


def first_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=UTC)
    offset = (4 - d.weekday()) % 7  # 金曜 = weekday 4
    return d + timedelta(days=offset)


def nfp_events(start: datetime, months: int = 6) -> list[EconomicEvent]:
    """米雇用統計(NFP)をルール生成: 毎月第 1 金曜。

    発表時刻は米東部 8:30。夏時間(概ね 4〜10 月)は 12:30 UTC、冬時間は 13:30 UTC。
    DST 境界(3 月・11 月)は月単位の近似(±1 時間の誤差は抑制ウィンドウ幅で吸収)。
    """
    events: list[EconomicEvent] = []
    year, month = start.year, start.month
    for _ in range(months):
        day = first_friday(year, month)
        hour = 12 if 4 <= month <= 10 else 13
        ts = day.replace(hour=hour, minute=30)
        events.append(
            EconomicEvent(
                id=f"nfp-{year}-{month:02d}",
                label="米雇用統計(NFP)",
                importance="hi",
                ts=ts,
                window_before_min=30,
                window_after_min=30,
                note="ルール生成(第1金曜)。DST 境界月は時刻±1h の近似",
            )
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return events


def load_static_events(path: Path) -> list[EconomicEvent]:
    if not path.exists():
        logger.warning("カレンダーファイルがありません: %s", path)
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    events: list[EconomicEvent] = []
    for item in raw.get("events", []):
        try:
            events.append(
                EconomicEvent(
                    id=item["id"],
                    label=item["label"],
                    importance=item["importance"],
                    ts=datetime.fromisoformat(item["ts"]).astimezone(UTC),
                    window_before_min=int(item.get("window_before_min", 0)),
                    window_after_min=int(item.get("window_after_min", 0)),
                    note=item.get("note", ""),
                )
            )
        except (KeyError, ValueError) as e:
            logger.error("カレンダー項目を読み飛ばしました(%s): %s", e, item)
    return events


class EconomicCalendar:
    """静的イベント+ルール生成イベントを統合して提供する。"""

    def __init__(self, path: Path | None = None, *, now: datetime | None = None) -> None:
        self._path = path or DEFAULT_CALENDAR_PATH
        base_now = now or utcnow()
        static = load_static_events(self._path)
        generated = nfp_events(base_now.replace(day=1), months=7)
        # 同一 id は静的定義を優先(手動上書きを可能にする)
        seen = {e.id for e in static}
        merged = static + [e for e in generated if e.id not in seen]
        self._events = sorted(merged, key=lambda e: e.ts)

    @property
    def events(self) -> list[EconomicEvent]:
        return list(self._events)

    def upcoming(self, now: datetime | None = None, limit: int = 5) -> list[EconomicEvent]:
        """今後の予定(抑制ウィンドウが未終了のものを含む)。"""
        now = now or utcnow()
        return [
            e for e in self._events
            if e.ts + timedelta(minutes=e.window_after_min) >= now
        ][:limit]

    def active_events(self, now: datetime | None = None) -> list[EconomicEvent]:
        """抑制ウィンドウが現在アクティブなイベント。"""
        now = now or utcnow()
        return [e for e in self._events if e.window_active(now)]
