"""schedule_engine 单元测试:按需展开、日界时区、EXDATE、提醒窗口边界。"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astrbot_plugin_course.schedule_engine import (
    SHANGHAI_TZ,
    day_events,
    upcoming_within_15m,
    week_start,
)
from astrbot_plugin_course.course_types import CourseSeries

SH = SHANGHAI_TZ
UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def series(dtstart, summary="课", rrule=None, all_day=False, exdates=frozenset()):
    return CourseSeries(
        summary=summary,
        dtstart=dtstart,
        duration=timedelta(minutes=95),
        location="E13",
        rrule_text=rrule,
        all_day=all_day,
        exdates_utc=exdates,
    )


class TestDayEvents:
    def test_single_day_occurrence(self):
        s = series(datetime(2026, 9, 7, 10, 40, tzinfo=SH))
        assert len(day_events([s], date(2026, 9, 7))) == 1
        assert day_events([s], date(2026, 9, 6)) == []
        assert day_events([s], date(2026, 9, 8)) == []

    def test_past_dates_included(self):
        """回归:周中查询本周一(已过去的日期)的课仍可见。"""
        s = series(datetime(2026, 9, 7, 10, 40, tzinfo=SH))
        assert len(day_events([s], date(2026, 9, 7))) == 1

    def test_rrule_count_semantics(self):
        """COUNT 从 DTSTART 起算:8/31 起 COUNT=3 → 8/31、9/7、9/14。"""
        s = series(datetime(2026, 8, 31, 10, 40, tzinfo=SH), rrule="FREQ=WEEKLY;COUNT=3")
        assert len(day_events([s], date(2026, 8, 31))) == 1
        assert len(day_events([s], date(2026, 9, 7))) == 1
        assert len(day_events([s], date(2026, 9, 14))) == 1
        assert len(day_events([s], date(2026, 9, 28))) == 0

    def test_infinite_rrule_safe(self):
        s = series(datetime(2026, 9, 7, 10, 40, tzinfo=SH), rrule="FREQ=WEEKLY")
        assert len(day_events([s], date(2026, 9, 7))) == 1
        assert len(day_events([s], date(2027, 3, 1))) == 1

    def test_exdate_excluded(self):
        dtstart = datetime(2026, 9, 7, 10, 40, tzinfo=SH)
        ex = (dtstart + timedelta(days=7)).astimezone(UTC)
        s = series(dtstart, rrule="FREQ=WEEKLY;COUNT=3", exdates=frozenset([ex]))
        assert len(day_events([s], date(2026, 9, 7))) == 1
        assert len(day_events([s], date(2026, 9, 14))) == 0
        assert len(day_events([s], date(2026, 9, 21))) == 1

    def test_multiple_series_sorted(self):
        late = series(datetime(2026, 9, 7, 18, 30, tzinfo=SH), summary="晚课")
        early = series(datetime(2026, 9, 7, 8, 0, tzinfo=SH), summary="早课")
        day = day_events([late, early], date(2026, 9, 7))
        assert [e.summary for e in day] == ["早课", "晚课"]

    def test_day_boundary_by_tz(self):
        s = series(datetime(2026, 9, 7, 20, 0, tzinfo=UTC))  # UTC 9/7 20:00
        # 上海 = 9/8 04:00;纽约 = 9/7 16:00
        assert len(day_events([s], date(2026, 9, 8), SH)) == 1
        assert len(day_events([s], date(2026, 9, 7), SH)) == 0
        assert len(day_events([s], date(2026, 9, 7), NY)) == 1
        assert len(day_events([s], date(2026, 9, 8), NY)) == 0

    def test_all_day_multi_day_duration(self):
        s = series(datetime(2026, 9, 7, 0, 0, tzinfo=SH), all_day=True, rrule=None)
        s = CourseSeries(
            summary=s.summary, dtstart=s.dtstart, duration=timedelta(days=3),
            all_day=True,
        )
        assert len(day_events([s], date(2026, 9, 7))) == 1


class TestUpcoming:
    def test_boundaries(self):
        now = datetime.now(SH)
        for minutes, expect in [(14, 1), (15, 1), (16, 0), (0, 0), (-1, 0)]:
            s = series(now + timedelta(minutes=minutes))
            hits = upcoming_within_15m(
                now=now, user_id="u", events=[s], advance_minutes=15
            )
            assert len(hits) == expect, f"minutes={minutes}"

    def test_all_day_filtered(self):
        now = datetime.now(SH)
        s = series(now + timedelta(minutes=10), all_day=True)
        assert upcoming_within_15m(
            now=now, user_id="u", events=[s], advance_minutes=15
        ) == []

    def test_default_advance_is_15(self):
        now = datetime.now(SH)
        s = series(now + timedelta(minutes=10))
        hits = upcoming_within_15m(now=now, user_id="u", events=[s])
        assert hits[0].event.summary == "课"
        assert hits[0].user_id == "u"


def test_week_start():
    assert week_start(date(2026, 9, 9)) == date(2026, 9, 7)  # 周三 → 周一
    assert week_start(date(2026, 9, 7)) == date(2026, 9, 7)  # 周一 → 自身
    assert week_start(date(2026, 9, 13)) == date(2026, 9, 7)  # 周日 → 本周一
