from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from icalendar import Calendar
from dateutil.rrule import rrulestr

from .course_types import CourseSeries, SHANGHAI_TZ, decode_bytes_to_text

_UTC = timezone.utc


def _attach_tz(dt: datetime, tz) -> datetime:
    """给 naive 时间附加时区;aware 时间保留其原时区(不再强制转换)。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def _collect_exdates(component, default_tz=SHANGHAI_TZ) -> frozenset[datetime]:
    """收集 VEVENT 的 EXDATE(取消的课次),统一转成 UTC。

    兼容单行多值、多个 EXDATE 行等写法;naive 值按 default_tz 解释,
    纯日期值按 default_tz 当天 0 点处理。
    """
    out: set[datetime] = set()
    prop = component.get("exdate")
    if prop is None:
        return frozenset()

    items = prop if isinstance(prop, (list, tuple)) else [prop]
    for item in items:
        dts = getattr(item, "dts", None)
        raw_list = (
            [getattr(v, "dt", v) for v in dts]
            if dts
            else [getattr(item, "dt", None)]
        )
        for value in raw_list:
            if isinstance(value, datetime):
                out.add(_attach_tz(value, default_tz).astimezone(_UTC))
            elif isinstance(value, date):
                out.add(
                    datetime.combine(value, dt_time.min, tzinfo=default_tz).astimezone(_UTC)
                )
    return frozenset(out)


class IcsParser:
    """把 ics 解析成“课程规则”列表,不预先展开重复课程。

    重复课程(RRULE)只保存规则本身;某一天有没有课、某时段是否开课,
    由 schedule_engine 在查询时按目标区间按需展开。好处:

    - 展开区间=查询区间:本周已过去的日期(如周一)不会被裁掉;
    - 不带 COUNT/UNTIL 的无限重复规则也不会被无限展开;
    - 缓存的是规则而非“按天展开的快照”,不会因跨天/换周而过时;
    - 缓存的是规则而非“按天展开的快照”,不会因跨天/换周而过时;
    - 文件缺失或格式非法时返回 None,与“解析成功但没有课程”([])相区分;
    - default_tz 用于解释无 TZID 的 naive 时间(默认东八区);带 TZID 的事件保留原时区。
    """

    def __init__(self):
        # (ics 路径, 默认时区名) -> (文件 mtime, 规则列表);文件被覆盖(mtime 变化)后自动重新解析
        self._cache: dict[tuple[str, str], tuple[float, list[CourseSeries]]] = {}

    def clear_cache(self, ics_path: str) -> None:
        for key in [k for k in self._cache if k[0] == ics_path]:
            self._cache.pop(key, None)

    def parse_ics_file(
        self, file_path: str, default_tz=SHANGHAI_TZ
    ) -> Optional[list[CourseSeries]]:
        """解析 ics 为课程规则列表。

        返回 None 表示文件缺失或格式非法(调用方应提示用户重新绑定);
        返回空列表仅表示文件有效但其中没有任何课程。
        default_tz:解释无 TZID 的 naive 时间的默认时区(默认东八区)。
        """
        try:
            mtime = Path(file_path).stat().st_mtime
        except OSError as e:
            logger.error(f"[course] cannot stat ics: {e}")
            return None

        tz_name = getattr(default_tz, "key", None) or str(default_tz)
        cache_key = (file_path, tz_name)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        try:
            raw = Path(file_path).read_bytes()
        except Exception as e:
            logger.error(f"[course] cannot read ics: {e}")
            return None

        cal_content = decode_bytes_to_text(raw)
        if cal_content is None:
            logger.error(
                f"[course] ics file {file_path} uses an unsupported encoding"
            )
            return None

        try:
            cal = Calendar.from_ical(cal_content)
        except Exception as e:
            logger.error(f"[course] invalid ics format: {e}")
            return None

        series_list: list[CourseSeries] = []

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            try:
                summary = str(component.get("summary") or "")
                description = str(component.get("description") or "")
                location = str(component.get("location") or "")
                dtstart_obj = component.get("dtstart")
                dtend_obj = component.get("dtend")
                if not dtstart_obj:
                    logger.warning("[course] skip invalid vevent: missing dtstart")
                    continue
                raw_start = dtstart_obj.dt
                raw_end = dtend_obj.dt if dtend_obj else None

                # 全天事件:DTSTART 为纯日期(DATE 型)。RFC 5545 规定 DTEND 为
                # 排他结束日期,缺省时默认持续 1 天。
                all_day = isinstance(raw_start, date) and not isinstance(
                    raw_start, datetime
                )

                if all_day:
                    if raw_end is None:
                        days = 1
                    elif isinstance(raw_end, date) and not isinstance(raw_end, datetime):
                        days = (raw_end - raw_start).days
                    else:
                        days = (raw_end.date() - raw_start).days
                    duration = timedelta(days=days if days >= 1 else 1)
                    dtstart = datetime.combine(
                        raw_start, dt_time.min, tzinfo=default_tz
                    )
                else:
                    if raw_end is None:
                        logger.warning("[course] skip invalid vevent: missing dtend")
                        continue
                    if isinstance(raw_end, date) and not isinstance(raw_end, datetime):
                        raw_end = datetime.combine(raw_end, dt_time.min)
                    dtstart = _attach_tz(raw_start, default_tz)
                    dtend = _attach_tz(raw_end, default_tz)
                    duration = dtend - dtstart

                rrule_prop = component.get("rrule")
                rrule_text: Optional[str] = None
                if rrule_prop is not None:
                    # RFC 5545 要求 UNTIL 与 DTSTART 类型一致;这里把日期型 UNTIL
                    # 补成当天最末时刻并转成 UTC,避免 dateutil 解析报错。
                    if "UNTIL" in rrule_prop:
                        until_dt = rrule_prop["UNTIL"][0]
                        if isinstance(until_dt, date) and not isinstance(
                            until_dt, datetime
                        ):
                            until_dt = datetime.combine(until_dt, dt_time.max)
                        if until_dt.tzinfo is None:
                            until_dt = until_dt.replace(tzinfo=default_tz)
                        rrule_prop["UNTIL"][0] = until_dt.astimezone(_UTC)
                    rrule_text = rrule_prop.to_ical().decode()

                series_list.append(
                    CourseSeries(
                        summary=summary,
                        dtstart=dtstart,
                        duration=duration,
                        location=location,
                        description=description,
                        rrule_text=rrule_text,
                        exdates_utc=_collect_exdates(component, default_tz),
                        all_day=all_day,
                    )
                )
            except Exception as e:
                logger.warning(f"[course] skip invalid vevent: {e}")
                continue

        series_list.sort(key=lambda s: s.dtstart)
        self._cache[cache_key] = (mtime, series_list)
        return series_list
