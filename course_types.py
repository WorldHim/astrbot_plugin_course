from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import FrozenSet, Optional

# 默认时区:东八区。全插件统一从本模块导出(单一来源)。
SHANGHAI_TZ = timezone(timedelta(hours=8))


def resolve_timezone(name: str):
    """按 IANA 名称解析时区;无效名称回退东八区。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return SHANGHAI_TZ


@dataclass(frozen=True)
class CourseSeries:
    """一条课程规则(对应 ics 里的单个 VEVENT)。

    不在解析阶段展开重复课程;具体某一天是否开课由 schedule_engine
    按查询区间按需展开。这样展开区间永远等于查询区间,不存在
    “预展开窗口”的边界问题(过去的日子不会被裁掉,无限重复也安全)。
    """

    summary: str
    dtstart: datetime  # 首次开始时间(东八区)
    duration: timedelta  # 单节课程时长
    location: str = ""
    description: str = ""
    rrule_text: Optional[str] = None  # None 表示单次日程,无重复
    # EXDATE 排除(取消)的具体出现时刻,统一为 UTC,便于与展开结果精确比较
    exdates_utc: FrozenSet[datetime] = frozenset()
    all_day: bool = False  # 全天事件(DTSTART 为纯日期,展示为「全天」)


@dataclass(frozen=True)
class CourseEvent:
    summary: str
    start_time: datetime
    end_time: datetime
    location: str = ""
    description: str = ""
    all_day: bool = False  # 全天事件,展示为「全天」

    def reminder_key(self) -> str:
        return "|".join(
            [
                self.start_time.isoformat(),
                self.end_time.isoformat(),
                self.summary,
                self.location,
            ]
        )


@dataclass
class UserBinding:
    user_id: str
    unified_msg_origin: str
    nickname: str
    ics_file: str
    updated_at_ts: float
    # User-configurable options
    enable_daily_push: bool = False
    daily_push_time: str = "07:00"  # Format: HH:MM
    enable_reminder: bool = False  # 开课提醒开关(默认关闭,需用 /设置提醒时间 开启)
    reminder_advance_minutes: int = 15
    daily_push_job_id: str = ""
    timezone_name: str = "Asia/Shanghai"  # 用户所在时区(IANA 名称)

    def get_timezone(self):
        """用户所在时区;配置无效时回退东八区。"""
        return resolve_timezone(self.timezone_name)
