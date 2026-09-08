"""schedule_engine 单元测试:按需展开、日界时区、EXDATE、提醒窗口边界。"""
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from astrbot_plugin_course.schedule_engine import (
    SHANGHAI_TZ,
    class_time_in_window,
    current_or_next_event,
    day_events,
    upcoming_within_15m,
    week_events,
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


class TestCurrentOrNext:
    """current_or_next_event:正在上的课与下一节课的判定。"""

    def test_current_ongoing(self):
        now = datetime.now(SH)
        s = series(now - timedelta(minutes=10), summary="正在上的课")
        current, upcoming = current_or_next_event([s], now, SH)
        assert current is not None and current.summary == "正在上的课"
        assert upcoming is None  # 单次日程没有后续

    def test_upcoming_today(self):
        now = datetime.now(SH)
        s = series(now + timedelta(minutes=30), summary="下一节课")
        current, upcoming = current_or_next_event([s], now, SH)
        assert current is None
        assert upcoming is not None and upcoming.summary == "下一节课"

    def test_upcoming_rrule_next_day(self):
        tomorrow_8am = (
            datetime.now(SH) + timedelta(days=1)
        ).replace(hour=8, minute=0, second=0, microsecond=0)
        s = series(tomorrow_8am, summary="明早的课", rrule="FREQ=DAILY")
        current, upcoming = current_or_next_event([s], datetime.now(SH), SH)
        assert current is None
        assert upcoming is not None and upcoming.summary == "明早的课"
        assert upcoming.start_time.date() == tomorrow_8am.date()

    def test_nothing_within_search_days(self):
        s = series(datetime.now(SH) + timedelta(days=10))
        current, upcoming = current_or_next_event(
            [s], datetime.now(SH), SH, search_days=7
        )
        assert current is None and upcoming is None

    def test_ongoing_across_midnight(self):
        """昨天 23:30 开始、95 分钟长的课,今天 00:25 仍判定为"正在上"。"""
        now = datetime(2026, 9, 8, 0, 25, tzinfo=SH)
        s = series(datetime(2026, 9, 7, 23, 30, tzinfo=SH))
        current, _ = current_or_next_event([s], now, SH)
        assert current is not None and current.summary == "课"

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


class TestBuildRuleCache:
    """C14:RRULE 解析结果按 (文本, DTSTART) 缓存复用。"""

    def test_same_key_returns_same_object(self):
        from astrbot_plugin_course.schedule_engine import _build_rule

        dt = datetime(2026, 9, 7, 10, 40, tzinfo=SH).astimezone(UTC)
        r1 = _build_rule("FREQ=WEEKLY;COUNT=3", dt.isoformat())
        r2 = _build_rule("FREQ=WEEKLY;COUNT=3", dt.isoformat())
        assert r1 is r2

    def test_day_events_reuses_cached_rule(self):
        """7 次逐日查询只解析一次规则(lru 命中)。"""
        from astrbot_plugin_course.schedule_engine import _build_rule

        _build_rule.cache_clear()
        ss = [
            series(datetime(2026, 9, 7, h, 0, tzinfo=SH),
                   summary=f"课{h}", rrule="FREQ=WEEKLY;COUNT=20")
            for h in (8, 10, 14)
        ]
        for i in range(7):
            day_events(ss, date(2026, 9, 7) + timedelta(days=i), SH)
        assert _build_rule.cache_info().misses == 3  # 3 条规则,而非 3×7


class TestWeekEvents:
    def test_matches_day_events(self):
        s = series(datetime(2026, 9, 7, 10, 40, tzinfo=SH), rrule="FREQ=WEEKLY;COUNT=4")
        week_lists = week_events([s], date(2026, 9, 7), SH)
        assert len(week_lists) == 7
        for i in range(7):
            d = date(2026, 9, 7) + timedelta(days=i)
            assert week_lists[i] == day_events([s], d, SH)

    def test_empty_series(self):
        week_lists = week_events([], date(2026, 9, 7), SH)
        assert len(week_lists) == 7
        assert all(day == [] for day in week_lists)

    def test_single_parse_for_whole_week(self):
        """整周一次展开:3 条规则只解析 3 次(逐天将解析 21 次)。"""
        from astrbot_plugin_course.schedule_engine import _build_rule

        _build_rule.cache_clear()
        ss = [
            series(datetime(2026, 9, 7, h, 0, tzinfo=SH),
                   summary=f"课{h}", rrule="FREQ=WEEKLY;COUNT=20")
            for h in (8, 10, 14)
        ]
        week_lists = week_events(ss, date(2026, 9, 7), SH)
        assert _build_rule.cache_info().misses == 3
        assert sum(len(day) for day in week_lists) == 3

    def test_event_on_sunday_belong_to_that_week(self):
        s = series(datetime(2026, 9, 13, 10, 0, tzinfo=SH))  # 周日
        week_lists = week_events([s], date(2026, 9, 7), SH)
        assert len(week_lists[6]) == 1  # 周日位置


class TestClassTimeInWindow:
    """class_time_in_window:统计窗口内的上课总时长与课次数。"""

    @staticmethod
    def _day_window(day: date, tz=SH):
        start = datetime.combine(day, dt_time.min, tzinfo=tz)
        return start, start + timedelta(days=1)

    def test_sums_courses_in_window(self):
        day = datetime.now(SH).date()
        courses = [
            series(datetime.combine(day, dt_time(9, 0), tzinfo=SH), summary="数学"),
            series(datetime.combine(day, dt_time(14, 0), tzinfo=SH), summary="英语"),
        ]
        secs, count = class_time_in_window(
            courses, *self._day_window(day), SH
        )
        assert (secs, count) == (2 * 95 * 60, 2)

    def test_clips_course_crossing_window_start(self):
        """昨天深夜开课、延续到今天凌晨的课:只计入今天窗口内的部分。"""
        today = datetime.now(SH).date()
        yesterday_start = datetime.combine(
            today - timedelta(days=1), dt_time(23, 30), tzinfo=SH
        )  # 23:30 开课,95 分钟 → 次日 01:05 结束
        s = series(yesterday_start)
        secs, count = class_time_in_window([s], *self._day_window(today), SH)
        assert (secs, count) == (65 * 60, 1)  # 只计今天 00:00-01:05 的 65 分钟

    def test_course_ending_before_window_not_counted(self):
        today = datetime.now(SH).date()
        s = series(
            datetime.combine(today - timedelta(days=2), dt_time(9, 0), tzinfo=SH)
        )
        assert class_time_in_window([s], *self._day_window(today), SH) == (0, 0)

    def test_all_day_event_not_counted(self):
        """全天事件没有精确时长,不计入时长榜。"""
        day = datetime.now(SH).date()
        s = series(datetime.combine(day, dt_time.min, tzinfo=SH), all_day=True)
        assert class_time_in_window([s], *self._day_window(day), SH) == (0, 0)

    def test_exdate_cancelled_occurrence_not_counted(self):
        today = datetime.now(SH).date()
        start = datetime.combine(today, dt_time(9, 0), tzinfo=SH)
        s = series(start, exdates=frozenset({start.astimezone(timezone.utc)}))
        assert class_time_in_window([s], *self._day_window(today), SH) == (0, 0)

    def test_week_window_sums_daily_course(self):
        """周窗口:DAILY 课程每天都有一节,整周 7 节全部计入。"""
        monday = week_start(datetime.now(SH).date())
        s = series(
            datetime.combine(monday, dt_time(9, 0), tzinfo=SH),
            rrule="FREQ=DAILY",
        )
        win_start = datetime.combine(monday, dt_time.min, tzinfo=SH)
        win_end = win_start + timedelta(days=7)
        secs, count = class_time_in_window([s], win_start, win_end, SH)
        assert (secs, count) == (7 * 95 * 60, 7)

