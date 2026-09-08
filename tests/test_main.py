"""main.py 轻量单元测试:配置读取、视图/兜底文案、时区解析、渲染缓存。"""
import asyncio
from datetime import datetime, timedelta

import pytest

from astrbot_plugin_course.course_types import CourseEvent
from astrbot_plugin_course.main import (
    _day_text_fallback,
    _event_view,
    _help_text,
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


class TestRenderCache:
    """C16:渲染结果按内容 hash 缓存,相同内容复用图片。"""

    DATA = {"title": "今日课表", "subtitle": "t | 2026-09-08", "courses": [], "page_width": 500}

    def test_cache_hit_skips_render(self, plugin):
        calls = []

        async def fake_render(template, data, options=None):
            calls.append(data)
            return "http://fake/1.png"

        plugin.html_render = fake_render
        url1 = asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        url2 = asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        assert url1 == url2 == "http://fake/1.png"
        assert len(calls) == 1  # 第二次命中缓存,未再渲染

    def test_data_change_renders_again(self, plugin):
        calls = []

        async def fake_render(template, data, options=None):
            calls.append(data)
            return f"http://fake/{len(calls)}.png"

        plugin.html_render = fake_render
        asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        changed = dict(self.DATA)
        changed["subtitle"] = "t | 2026-09-15"  # 内容变化
        asyncio.run(plugin._render_schedule("T", changed))
        assert len(calls) == 2

    def test_ttl_expiry_renders_again(self, plugin):
        calls = []

        async def fake_render(template, data, options=None):
            calls.append(data)
            return "http://fake/x.png"

        plugin.html_render = fake_render
        asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        # 把唯一缓存条目的过期时间改到过去 → TTL 过期
        for k, (exp, url) in plugin._render_cache.items():
            plugin._render_cache[k] = (0, url)
        asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        assert len(calls) == 2  # 过期后重新渲染

    def test_cache_disabled(self, plugin):
        plugin._config = {"render_cache_minutes": 0}
        calls = []

        async def fake_render(template, data, options=None):
            calls.append(data)
            return "http://fake/x.png"

        plugin.html_render = fake_render
        asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        assert len(calls) == 2  # 禁用缓存 → 每次都渲染
        assert plugin._render_cache == {}

    def test_failure_not_cached(self, plugin):
        state = {"fail": True}
        calls = []

        async def flaky_render(template, data, options=None):
            calls.append(data)
            if state["fail"]:
                raise RuntimeError("boom")
            return "http://fake/ok.png"

        plugin.html_render = flaky_render
        assert asyncio.run(plugin._render_schedule("T", dict(self.DATA))) is None
        state["fail"] = False
        url = asyncio.run(plugin._render_schedule("T", dict(self.DATA)))
        assert url == "http://fake/ok.png"  # 失败未缓存,恢复后成功
        assert len(calls) == 2

    def test_all_views_cached_independently(self, plugin):
        """今日/明日/本周/下周四个视图各查两次:只渲染 4 次,且互不串缓存。"""
        calls = []

        async def fake_render(template, data, options=None):
            calls.append((template, data["title"]))
            return f"http://fake/{len(calls)}.png"

        plugin.html_render = fake_render
        views = [
            ("DAY", {"title": "今日课表", "subtitle": "Alice | 2026-09-08", "courses": [], "page_width": 500}),
            ("DAY", {"title": "明日课表", "subtitle": "Alice | 2026-09-09", "courses": [], "page_width": 500}),
            ("WEEK", {"title": "本周课表", "subtitle": "Alice | 2026-09-07 ~ 2026-09-13", "courses": [], "page_width": 1280}),
            ("WEEK", {"title": "下周课表", "subtitle": "Alice | 2026-09-14 ~ 2026-09-20", "courses": [], "page_width": 1280}),
        ]
        for tmpl, data in views:
            asyncio.run(plugin._render_schedule(tmpl, dict(data)))
            asyncio.run(plugin._render_schedule(tmpl, dict(data)))  # 重复查询 → 命中缓存
        assert len(calls) == 4  # 每个视图只渲染一次
        assert len(plugin._render_cache) == 4  # 四个视图各自独立缓存


class TestHelpText:
    """帮助指令文本完整性:全部指令均被列出。"""

    def test_contains_all_commands(self):
        text = _help_text()
        for cmd in [
            "绑定课表",
            "删除课表",
            "今日课表",
            "明日课表",
            "本周课表",
            "下周课表",
            "设置每日推送",
            "设置提醒时间",
            "设置时区",
            "查看设置",
            "课表帮助",
        ]:
            assert f"/{cmd}" in text, f"帮助文本缺少指令 /{cmd}"

    def test_grouped_with_tip(self):
        text = _help_text()
        for section in ["【课表管理】", "【课表查询】", "【配置管理】"]:
            assert section in text
        assert "课表转日历" in text  # 课表文件导出工具(WikiLake)提示

