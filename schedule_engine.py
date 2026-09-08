from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import List, Sequence

from dateutil.rrule import rrulestr

from .course_types import CourseEvent, CourseSeries


SHANGHAI_TZ = timezone(timedelta(hours=8))
_UTC = timezone.utc


def _to_event(series: CourseSeries, occ_utc: datetime) -> CourseEvent:
    local = occ_utc.astimezone(SHANGHAI_TZ)
    return CourseEvent(
        summary=series.summary,
        start_time=local,
        end_time=local + series.duration,
        location=series.location,
        description=series.description,
    )


def _expand_series(
    series_list: Sequence[CourseSeries],
    win_start_utc: datetime,
    win_end_utc: datetime,
) -> List[CourseEvent]:
    """把课程规则展开到指定时间窗内(区间由调用方即查询方决定)。

    - 重复课程用 dateutil 的 between 只展开窗口内次数:不带 COUNT/UNTIL
      的无限重复也安全,过去/未来的日期都不会被“预展开窗口”裁掉;
    - EXDATE 排除(取消)的课次会被过滤。
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
                events.append(_to_event(series, dtstart_utc))
            continue

        # 重复课程:按需展开,展开区间 = 本次查询区间
        rule = rrulestr(series.rrule_text, dtstart=dtstart_utc)
        for occ_utc in rule.between(win_start_utc, win_end_utc, inc=True):
            if occ_utc in series.exdates_utc:
                continue  # EXDATE 取消的课次
            events.append(_to_event(series, occ_utc))

    events.sort(key=lambda e: e.start_time)
    return events


def day_events(series_list: Sequence[CourseSeries], target_date: date) -> List[CourseEvent]:
    """取东八区自然日 target_date 的课程(含重复展开,含已过去的日期)。"""
    day_start = datetime.combine(
        target_date, dt_time.min, tzinfo=SHANGHAI_TZ
    ).astimezone(_UTC)
    day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
    return _expand_series(series_list, day_start, day_end)


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
) -> List[ReminderHit]:
    """按需展开 (now, now+advance] 内开课的课程;命中条件与旧版完全一致。

    events 传入 IcsParser.parse_ics_file() 输出的课程规则列表。
    """
    now_utc = now.astimezone(_UTC)
    win_end = now_utc + timedelta(minutes=advance_minutes)
    expanded = _expand_series(events, now_utc + timedelta(microseconds=1), win_end)
    return [ReminderHit(user_id=user_id, event=e) for e in expanded]
