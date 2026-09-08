from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import List, Sequence

from dateutil.rrule import rrulestr

from .course_types import CourseEvent, CourseSeries, SHANGHAI_TZ

_UTC = timezone.utc


@lru_cache(maxsize=1024)
def _build_rule(rrule_text: str, dtstart_iso: str):
    """构建并缓存 RRULE 解析结果(同一规则全插件只解析一次)。"""
    return rrulestr(rrule_text, dtstart=datetime.fromisoformat(dtstart_iso))


def _to_event(series: CourseSeries, occ_utc: datetime, tz: tzinfo = SHANGHAI_TZ) -> CourseEvent:
    local = occ_utc.astimezone(tz)
    return CourseEvent(
        summary=series.summary,
        start_time=local,
        end_time=local + series.duration,
        location=series.location,
        description=series.description,
        all_day=series.all_day,
    )


def _expand_series(
    series_list: Sequence[CourseSeries],
    win_start_utc: datetime,
    win_end_utc: datetime,
    tz: tzinfo = SHANGHAI_TZ,
) -> List[CourseEvent]:
    """把课程规则展开到指定时间窗内(区间由调用方即查询方决定)。

    - 重复课程用 dateutil 的 between 只展开窗口内次数:不带 COUNT/UNTIL
      的无限重复也安全,过去/未来的日期都不会被“预展开窗口”裁掉;
    - EXDATE 排除(取消)的课次会被过滤;
    - tz 决定事件展示的当地时间(默认东八区,可传用户时区)。
    """
    events: List[CourseEvent] = []
    for series in series_list:
        dtstart_utc = series.dtstart.astimezone(_UTC)

        if series.rrule_text is None:
            # 单次日程:开始时间落在窗口内即保留
            if (
                win_start_utc <= dtstart_utc <= win_end_utc
                and dtstart_utc not in series.exdates_utc
            ):
                events.append(_to_event(series, dtstart_utc, tz))
            continue

        # 重复课程:按需展开,展开区间就是本次查询区间
        rule = _build_rule(series.rrule_text, dtstart_utc.isoformat())
        for occ_utc in rule.between(win_start_utc, win_end_utc, inc=True):
            if occ_utc in series.exdates_utc:
                continue  # EXDATE 取消的课次
            events.append(_to_event(series, occ_utc, tz))

    events.sort(key=lambda e: e.start_time)
    return events


def day_events(
    series_list: Sequence[CourseSeries],
    target_date: date,
    tz: tzinfo = SHANGHAI_TZ,
) -> List[CourseEvent]:
    """取 tz 时区自然日 target_date 的课程(默认东八区;含重复展开,含已过去的日期)。"""
    day_start = datetime.combine(
        target_date, dt_time.min, tzinfo=tz
    ).astimezone(_UTC)
    day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
    return _expand_series(series_list, day_start, day_end, tz)


def week_events(
    series_list: Sequence[CourseSeries],
    week_monday: date,
    tz: tzinfo = SHANGHAI_TZ,
) -> List[List[CourseEvent]]:
    """一次展开整周(周一起的 7 天)并按天分组。

    与连续 7 次 `day_events` 结果一致,但每条规则只做一次区间展开,
    解析与遍历开销降低约 7 倍。返回固定 7 个列表(周一~周日)。
    """
    week_start_dt = datetime.combine(
        week_monday, dt_time.min, tzinfo=tz
    ).astimezone(_UTC)
    week_end = week_start_dt + timedelta(days=7) - timedelta(microseconds=1)
    events = _expand_series(series_list, week_start_dt, week_end, tz)

    by_day: dict[date, List[CourseEvent]] = {}
    for event in events:
        by_day.setdefault(event.start_time.astimezone(tz).date(), []).append(event)

    return [
        by_day.get(week_monday + timedelta(days=i), []) for i in range(7)
    ]


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class ReminderHit:
    user_id: str
    event: CourseEvent


def upcoming_within_15m(
    *,
    now: datetime,
    user_id: str,
    events: Sequence[CourseSeries],
    advance_minutes: int = 15,
    tz: tzinfo = SHANGHAI_TZ,
) -> List[ReminderHit]:
    """按需展开 (now, now+advance] 内开课的课程;命中条件与旧版完全一致。

    events 传入 IcsParser.parse_ics_file() 输出的课程规则列表;
    tz 用于事件展示的当地时间(默认东八区,可传用户时区)。
    """
    now_utc = now.astimezone(_UTC)
    win_end = now_utc + timedelta(minutes=advance_minutes)
    expanded = _expand_series(
        events, now_utc + timedelta(microseconds=1), win_end, tz
    )
    # 全天课程没有精确的"即将开课"时刻,不参与开课提醒(避免深夜误报)
    return [
        ReminderHit(user_id=user_id, event=e)
        for e in expanded
        if not e.all_day
    ]
