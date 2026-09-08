"""main.py 轻量单元测试:配置读取、视图/兜底文案、时区解析。"""
from datetime import datetime, timedelta

import pytest

from astrbot_plugin_course.course_types import CourseEvent
from astrbot_plugin_course.main import (
    _day_text_fallback,
    _event_view,
    _resolve_timezone,
    _week_text_fallback,
)


def _event(minutes_offset: int = 10, **kwargs) -> CourseEvent:
    start = datetime(2026, 9, 7, 10, 40)
    return CourseEvent(
        summary="测试课",
        start_time=start,
        end_time=start + timedelta(minutes=95),
        location="E13-505",
        **kwargs,
    )


class TestEventView:
    def test_normal(self):
        e = CourseEvent(
            summary="测试课",
            start_time=datetime(2026, 9, 7, 10, 40),
            end_time=datetime(2026, 9, 7, 12, 15),
            location="E13-505",
        )
        assert _event_view(e) == {
            "summary": "测试课",
            "location": "E13-505",
            "time_range": "10:40 - 12:15",
        }

    def test_all_day(self):
        e = CourseEvent(
            summary="全天",
            start_time=datetime(2026, 9, 7),
            end_time=datetime(2026, 9, 8),
            all_day=True,
        )
        assert _event_view(e)["time_range"] == "全天"


class TestDayTextFallback:
    def test_contains_courses(self):
        courses = [{"time_range": "10:40 - 12:15", "summary": "微积分", "location": "E13"}]
        text = _day_text_fallback("今日课表", "t | 2026-09-08", courses)
        assert "图片渲染失败" in text
        assert "微积分" in text and "@E13" in text and "10:40" in text

    def test_empty_courses(self):
        text = _day_text_fallback("今日课表", "t | d", [])
        assert "无课" in text


class TestWeekTextFallback:
    def test_mixed_days(self):
        days = [
            {"label": "周一", "date": "09-07", "is_today": False, "courses": []},
            {"label": "周二", "date": "09-08", "is_today": True,
             "courses": [{"time_range": "18:30 - 20:05", "summary": "心理学", "location": "E13"}]},
        ]
        text = _week_text_fallback("本周课表", "t | a", days)
        assert "图片渲染失败" in text
        assert "【周一 09-07】 无课" in text
        assert "周二 09-08 · 今天" in text and "心理学" in text


class TestResolveTimezone:
    def test_valid(self):
        assert _resolve_timezone("America/New_York") is not None
        assert _resolve_timezone("Asia/Shanghai") is not None

    def test_invalid_returns_none(self):
        assert _resolve_timezone("Bogus/Zone") is None
        assert _resolve_timezone("") is None


class TestConfig:
    def test_defaults_without_config(self, plugin):
        assert plugin._cfg("render_quality", 100) == 100
        assert plugin._cfg_int("reminder_tick_seconds", 60, 5) == 60
        assert plugin._cfg_int("max_ics_mb", 5, 1) == 5

    def test_injection(self, plugin):
        plugin._config = {"render_quality": 85, "reminder_tick_seconds": 30, "max_ics_mb": 2}
        assert plugin._cfg_int("render_quality", 100, 1) == 85
        assert plugin._cfg_int("reminder_tick_seconds", 60, 5) == 30
        assert plugin._cfg_int("max_ics_mb", 5, 1) == 2

    def test_invalid_falls_back(self, plugin):
        plugin._config = {"render_quality": "abc", "reminder_tick_seconds": 0}
        assert plugin._cfg_int("render_quality", 100, 1) == 100
        assert plugin._cfg_int("reminder_tick_seconds", 60, 5) == 60

    def test_none_value_falls_back(self, plugin):
        plugin._config = {"default_push_time": None}
        assert plugin._cfg("default_push_time", "07:00") == "07:00"
