"""main.py 轻量单元测试:配置读取、视图/兜底文案、时区解析、渲染缓存。"""
import asyncio
from datetime import datetime, time as dt_time, timedelta, timezone

import pytest

from astrbot_plugin_course.course_types import CourseEvent, CourseSeries, SHANGHAI_TZ
from astrbot_plugin_course.help_content import TOOL_LINK, help_render_data, help_text
from astrbot_plugin_course.main import (
    PLUGIN_VERSION,
    _SenderSessionFilter,
    _avatar_for,
    _avatar_from_event,
    _avatar_url,
    _day_text_fallback,
    _event_view,
    _find_link_candidates,
    _format_rank_total,
    _group_now_text,
    _resolve_timezone,
    _study_rank_text,
    _week_text_fallback,
)
from conftest import FakeEvent, make_binding


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

    def test_viewport_height_injected(self, plugin):
        """渲染 options 自动注入极小 viewport 高度 → 图片长度随内容自适应。"""
        captured = {}

        async def fake_render(template, data, options=None):
            captured.update(options or {})
            return "http://fake/x.png"

        plugin.html_render = fake_render
        asyncio.run(plugin._render_schedule("T", dict(self.DATA), options={"quality": 88}))
        assert captured.get("viewport_height") == 8  # 内容决定图片长度
        assert captured.get("quality") == 88  # 调用方显式选项不被覆盖

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
        text = help_text()
        for cmd in [
            "绑定课表",
            "删除课表",
            "今日课表",
            "明日课表",
            "本周课表",
            "下周课表",
            "当前课程",
            "上课时长榜",
            "上课时长周榜",
            "设置每日推送",
            "设置提醒时间",
            "设置时区",
            "查看设置",
            "课表帮助",
        ]:
            assert f"/{cmd}" in text, f"帮助文本缺少指令 /{cmd}"

    def test_grouped_with_tip(self):
        text = help_text()
        for section in ["【课表管理】", "【课表查询】", "【配置管理】"]:
            assert section in text
        assert "课表转日历" in text  # 课表文件导出工具(WikiLake)提示


class TestHelpCommand:
    """帮助指令:图片渲染(同版本走缓存)与文字兜底。"""
    def test_render_data_structure(self):
        data = help_render_data(PLUGIN_VERSION)
        cmds = [c["cmd"] for s in data["sections"] for c in s["commands"]]
        assert len(cmds) == 14
        assert all(c.startswith("/") for c in cmds)
        assert data["version"] == PLUGIN_VERSION  # 版本号进缓存键:同版本同图
        assert data["title"]
        assert len(data["sections"]) == 3

    def test_help_cmd_renders_image_with_cache(self, plugin):
        calls = []

        async def fake_render(template, data, options=None):
            calls.append(data)
            return "http://fake/help.png"

        async def run_help():
            return [r async for r in plugin.help_cmd(FakeEvent("u1"))]

        plugin.html_render = fake_render
        r1 = asyncio.run(run_help())
        r2 = asyncio.run(run_help())
        assert r1 == [("image", "http://fake/help.png"), TOOL_LINK]
        assert r2 == r1
        assert len(calls) == 1  # 同版本帮助内容相同 → 命中渲染缓存,只渲染一次

    def test_help_cmd_falls_back_to_text(self, plugin):
        async def bad_render(template, data, options=None):
            raise RuntimeError("boom")

        async def run_help():
            return [r async for r in plugin.help_cmd(FakeEvent("u1"))]

        plugin.html_render = bad_render
        results = asyncio.run(run_help())
        assert len(results) == 2
        assert isinstance(results[0], str)
        assert "/绑定课表" in results[0]  # 文字兜底包含指令列表
        assert "wikilake" in results[1] and results[1] == TOOL_LINK  # 链接单独成条(可点击)


class TestReminderToggle:
    """开课提醒默认关闭:未开启的用户在提醒循环中被完全跳过。"""

    @staticmethod
    def _binding_with_upcoming_course(uid="u", minutes_ahead=10, *, enabled: bool):
        b = make_binding(uid, 15)
        b.enable_reminder = enabled
        return b, [
            CourseSeries(
                summary="测试课",
                dtstart=datetime.now(SHANGHAI_TZ) + timedelta(minutes=minutes_ahead),
                duration=timedelta(minutes=45),
                rrule_text="FREQ=DAILY",
            )
        ]

    def test_remind_user_skipped_when_disabled(self, plugin):
        from datetime import timezone

        binding, series = self._binding_with_upcoming_course("u", enabled=False)
        plugin._load_series = lambda b: series
        sent = []

        async def fake_send(session, chain):
            sent.append(chain)

        plugin._context.send_message = fake_send

        result = asyncio.run(
            plugin._remind_user("u", binding, datetime.now(timezone.utc))
        )
        assert result is False  # 未开启提醒:直接跳过,不发送
        assert sent == []

    def test_remind_user_sends_when_enabled(self, plugin):
        from datetime import timezone

        binding, series = self._binding_with_upcoming_course("u", enabled=True)
        plugin._load_series = lambda b: series
        sent = []

        async def fake_send(session, chain):
            sent.append(chain)

        plugin._context.send_message = fake_send

        result = asyncio.run(
            plugin._remind_user("u", binding, datetime.now(timezone.utc))
        )
        assert result is True
        assert len(sent) == 1
        assert "测试课" in sent[0].items[0]


class TestSenderSessionFilter:
    """群聊支持:会话等待按 (会话, 发送者) 界定,他人消息不干扰。"""

    @staticmethod
    def _event(origin: str, uid: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            unified_msg_origin=origin, get_sender_id=lambda: uid
        )

    def test_same_group_same_user_is_one_session(self):
        f = _SenderSessionFilter()
        a = f.filter(self._event("aiocqhttp:GroupMessage:123", "u1"))
        assert a == f.filter(self._event("aiocqhttp:GroupMessage:123", "u1"))

    def test_same_group_different_users_differ(self):
        f = _SenderSessionFilter()
        a = f.filter(self._event("aiocqhttp:GroupMessage:123", "u1"))
        b = f.filter(self._event("aiocqhttp:GroupMessage:123", "u2"))
        assert a != b  # 群聊中他人消息不会串入当前用户的会话

    def test_different_groups_differ(self):
        f = _SenderSessionFilter()
        a = f.filter(self._event("aiocqhttp:GroupMessage:123", "u1"))
        b = f.filter(self._event("aiocqhttp:GroupMessage:456", "u1"))
        assert a != b  # 不同群互相隔离

    def test_private_chat_keeps_working(self):
        f = _SenderSessionFilter()
        a = f.filter(self._event("aiocqhttp:FriendMessage:123", "u1"))
        b = f.filter(self._event("aiocqhttp:GroupMessage:123", "u1"))
        assert a != b  # 私聊与群聊隔离


class TestGroupSchedule:
    """/群课表:按课程分组展示本群成员此刻正在上的课(头像)。"""

    @staticmethod
    def _run(plugin, evt):
        async def run():
            return [r async for r in plugin.group_schedule(evt)]

        return asyncio.run(run())

    @staticmethod
    def _binding(uid, nickname, umo):
        b = make_binding(uid)
        b.nickname = nickname
        b.unified_msg_origin = umo
        return b

    def test_no_members_hints_bind(self, plugin):
        results = self._run(
            plugin, FakeEvent("x", unified_msg_origin="group:1")
        )
        assert len(results) == 1
        assert "本群还没有人绑定课表" in results[0]

    def test_collect_groups_by_course_with_avatar(self, plugin):
        from astrbot_plugin_course.course_types import CourseSeries, SHANGHAI_TZ as SH

        now_sh = datetime.now(SH)
        ongoing = [
            CourseSeries(
                summary="高等数学",
                dtstart=now_sh - timedelta(minutes=10),
                duration=timedelta(minutes=45),
                location="明理楼302",
            )
        ]
        plugin._storage.save_bindings(
            {
                "u1": self._binding("u1", "Alice", "group:1"),
                "u2": self._binding("u2", "Bob", "group:1"),
                "u3": self._binding("u3", "Carol", "group:1"),
            }
        )
        # u3 没课
        plugin._load_series = lambda b: ongoing if b.user_id != "u3" else []

        data = plugin._collect_group_now(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
        )
        assert len(data["groups"]) == 1
        g = data["groups"][0]
        assert g["summary"] == "高等数学"
        assert g["location"] == "明理楼302"
        assert [m["nickname"] for m in g["members"]] == ["Alice", "Bob"]
        assert g["members"][0]["avatar"] == "https://q1.qlogo.cn/g?b=qq&nk=u1&s=100"
        assert "还剩" in g["remain"] or g["remain"].endswith("分钟")

    def test_same_course_members_merged_into_one_group(self, plugin):
        from astrbot_plugin_course.course_types import CourseSeries, SHANGHAI_TZ as SH

        now_sh = datetime.now(SH)
        same = [
            CourseSeries(
                summary="同一门课",
                dtstart=now_sh - timedelta(minutes=5),
                duration=timedelta(minutes=95),
            )
        ]
        plugin._storage.save_bindings(
            {
                "u1": self._binding("u1", "Alice", "group:1"),
                "u2": self._binding("u2", "Bob", "group:1"),
            }
        )
        plugin._load_series = lambda b: same

        data = plugin._collect_group_now(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
        )
        assert len(data["groups"]) == 1  # 同课同时刻 → 聚合为一组
        assert len(data["groups"][0]["members"]) == 2

    def test_renders_image_with_fallback_text(self, plugin):
        from astrbot_plugin_course.course_types import CourseSeries, SHANGHAI_TZ as SH

        now_sh = datetime.now(SH)
        ongoing = [
            CourseSeries(
                summary="高等数学",
                dtstart=now_sh - timedelta(minutes=10),
                duration=timedelta(minutes=45),
                location="E13",
            )
        ]
        plugin._storage.save_bindings(
            {"u1": self._binding("u1", "Alice", "group:1")}
        )
        plugin._load_series = lambda b: ongoing

        # 渲染成功 → 图片
        async def ok_render(template, data, options=None):
            return "http://fake/group.png"

        plugin.html_render = ok_render
        results = self._run(
            plugin, FakeEvent("x", unified_msg_origin="group:1")
        )
        assert results == [("image", "http://fake/group.png")]

        # 渲染失败 → 文字兜底(同源数据);先清渲染缓存避免命中上次结果
        plugin._render_cache.clear()

        async def bad_render(template, data, options=None):
            raise RuntimeError("boom")

        plugin.html_render = bad_render
        results = self._run(
            plugin, FakeEvent("x", unified_msg_origin="group:1")
        )
        text = results[0]
        assert "高等数学" in text and "Alice" in text
        assert "还剩" in text

    def test_idle_member_not_shown(self, plugin):
        """没在上课的成员不出现在输出中。"""
        from astrbot_plugin_course.course_types import CourseSeries, SHANGHAI_TZ as SH

        now_sh = datetime.now(SH)
        ongoing = [
            CourseSeries(
                summary="正在上的课",
                dtstart=now_sh - timedelta(minutes=10),
                duration=timedelta(minutes=45),
            )
        ]
        plugin._storage.save_bindings(
            {
                "u1": self._binding("u1", "Alice", "group:1"),
                "u2": self._binding("u2", "IdleBob", "group:1"),  # 没课
            }
        )
        plugin._load_series = lambda b: ongoing if b.user_id == "u1" else []

        data = plugin._collect_group_now(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
        )
        assert [m["nickname"] for m in data["groups"][0]["members"]] == ["Alice"]
        text = _group_now_text(data)
        assert "IdleBob" not in text  # 没课成员完全不显示

    def test_member_with_broken_series_goes_idle(self, plugin):
        plugin._storage.save_bindings(
            {"u1": self._binding("u1", "Alice", "group:1")}
        )
        plugin._load_series = lambda b: None
        data = plugin._collect_group_now(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
        )
        assert data["groups"] == []  # 读取失败等同没课:不显示,仅剩空态

    def test_other_group_members_excluded(self, plugin):
        plugin._storage.save_bindings(
            {"u1": self._binding("u1", "Alice", "group:1")}
        )
        results = self._run(
            plugin, FakeEvent("x", unified_msg_origin="group:2")
        )
        assert "本群还没有人绑定课表" in results[0]
        assert "Alice" not in results[0]


class TestStudyRank:
    """/上课时长榜:本群成员日/周上课时长排行(图片)。"""

    @staticmethod
    def _binding(uid, nickname, umo="group:1"):
        b = make_binding(uid)
        b.nickname = nickname
        b.unified_msg_origin = umo
        return b

    @staticmethod
    def _run(plugin, evt, command="study_rank"):
        async def run():
            return [r async for r in getattr(plugin, command)(evt)]

        return asyncio.run(run())

    @staticmethod
    def _daily_courses(when: datetime, duration_minutes=95):
        return [
            CourseSeries(
                summary="高等数学",
                dtstart=when,
                duration=timedelta(minutes=duration_minutes),
                location="明理楼302",
            )
        ]

    def test_no_members_hints_bind(self, plugin):
        results = self._run(plugin, FakeEvent("x", unified_msg_origin="group:1"))
        assert len(results) == 1
        assert "本群还没有人绑定课表" in results[0]

    def test_daily_rank_sorted_with_avatar(self, plugin):
        """日榜:按总时长降序,含头像、节数与时长。"""
        from datetime import time as dt_time

        today = datetime.now(SHANGHAI_TZ).date()
        busy = self._daily_courses(
            datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        )
        second = self._daily_courses(
            datetime.combine(today, dt_time(14, 0), tzinfo=SHANGHAI_TZ)
        )
        plugin._storage.save_bindings(
            {
                "u1": self._binding("u1", "Alice"),
                "u2": self._binding("u2", "Bob"),
            }
        )
        # Alice 今天两节,Bob 只有一节
        plugin._load_series = lambda b: (busy + second) if b.user_id == "u1" else busy

        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "日",
        )
        assert data["title"] == "今日上课时长榜"
        assert [r["nickname"] for r in data["rows"]] == ["Alice", "Bob"]
        assert [r["rank"] for r in data["rows"]] == [1, 2]
        assert data["rows"][0]["count"] == 2
        assert data["rows"][1]["count"] == 1
        assert data["rows"][0]["avatar"] == "https://q1.qlogo.cn/g?b=qq&nk=u1&s=100"
        assert "小时" in data["rows"][0]["total"]

    def test_zero_duration_member_excluded(self, plugin):
        """今天没课的成员不上榜(课在明天),有课成员正常上榜。"""
        from datetime import time as dt_time

        tomorrow = datetime.now(SHANGHAI_TZ).date() + timedelta(days=1)
        today = datetime.now(SHANGHAI_TZ).date()
        tomorrow_course = self._daily_courses(
            datetime.combine(tomorrow, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        )
        today_course = self._daily_courses(
            datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        )
        plugin._storage.save_bindings(
            {
                "u1": self._binding("u1", "IdleBob"),
                "u2": self._binding("u2", "Alice"),
            }
        )
        # IdleBob 的课全在明天 → 今天时长 0,不上榜
        plugin._load_series = (
            lambda b: tomorrow_course if b.user_id == "u1" else today_course
        )

        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "日",
        )
        assert [r["nickname"] for r in data["rows"]] == ["Alice"]

    def test_tie_broken_by_nickname(self, plugin):
        """时长并列时按昵称排序。"""
        from datetime import time as dt_time

        today = datetime.now(SHANGHAI_TZ).date()
        same = self._daily_courses(
            datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        )
        plugin._storage.save_bindings(
            {
                "u1": self._binding("u1", "Bob"),
                "u2": self._binding("u2", "Alice"),
            }
        )
        plugin._load_series = lambda b: same

        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "日",
        )
        assert [r["nickname"] for r in data["rows"]] == ["Alice", "Bob"]
        assert [r["rank"] for r in data["rows"]] == [1, 2]

    def test_week_mode_sums_whole_week(self, plugin):
        """周榜:本周每天的课都计入,标题与日期范围正确。"""
        from datetime import time as dt_time

        from astrbot_plugin_course.schedule_engine import week_start

        monday = week_start(datetime.now(SHANGHAI_TZ).date())
        daily = self._daily_courses(
            datetime.combine(monday, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        )
        daily[0] = CourseSeries(
            summary="高等数学",
            dtstart=daily[0].dtstart,
            duration=daily[0].duration,
            location="明理楼302",
            rrule_text="FREQ=DAILY",
        )
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        plugin._load_series = lambda b: daily

        async def ok_render(template, data, options=None):
            return "http://fake/week_rank.png"

        plugin.html_render = ok_render
        results = self._run(
            plugin, FakeEvent("x", unified_msg_origin="group:1"), "study_week_rank"
        )
        assert results == [("image", "http://fake/week_rank.png")]

        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "周",
        )
        assert data["title"] == "本周上课时长榜"
        assert "~" in data["range"]  # 日期范围 09-07 ~ 09-13
        assert data["rows"][0]["count"] == 7  # 本周一到周日每天一节
        assert data["rows"][0]["total"] == "11 小时 5 分"  # 665 分钟

    def test_default_mode_is_day(self, plugin):
        """日榜命令固定统计各自时区的今天。"""
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        plugin._load_series = lambda b: []

        results = self._run(plugin, FakeEvent("x", unified_msg_origin="group:1"))
        assert len(results) == 1
        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "日",
        )
        assert data["title"] == "今日上课时长榜"
        assert data["rows"] == []  # 空课表 → 空榜

    def test_renders_image_with_fallback_text(self, plugin):
        """渲染成功发图片;失败退文字兜底(奖牌名次+节数)。"""
        from datetime import time as dt_time

        today = datetime.now(SHANGHAI_TZ).date()
        courses = self._daily_courses(
            datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        )
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        plugin._load_series = lambda b: courses

        async def ok_render(template, data, options=None):
            return "http://fake/rank.png"

        plugin.html_render = ok_render
        results = self._run(plugin, FakeEvent("x", unified_msg_origin="group:1"))
        assert results == [("image", "http://fake/rank.png")]

        plugin._render_cache.clear()  # 避免命中上次渲染缓存

        async def bad_render(template, data, options=None):
            raise RuntimeError("boom")

        plugin.html_render = bad_render
        results = self._run(plugin, FakeEvent("x", unified_msg_origin="group:1"))
        text = results[0]
        assert "🏆" in text and "今日上课时长榜" in text
        assert "🥇 Alice" in text and "共 1 节" in text

    def test_text_fallback_empty_rank(self, plugin):
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        plugin._load_series = lambda b: []
        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "日",
        )
        text = _study_rank_text(data)
        assert "本榜周期内暂无上课记录" in text

    def test_total_never_advances_to_days(self, plugin):
        """榜单总时长一律按小时结算,不进位到天。"""
        from datetime import time as dt_time

        from astrbot_plugin_course.schedule_engine import week_start

        assert _format_rank_total(45) == "45 分钟"
        assert _format_rank_total(60) == "1 小时"
        assert _format_rank_total(25 * 60) == "25 小时"
        assert _format_rank_total(26 * 60 + 30) == "26 小时 30 分"

        # 周榜集成:每天 4 节 95 分钟,整周 2660 分钟 → "44 小时 20 分"
        monday = week_start(datetime.now(SHANGHAI_TZ).date())
        courses = [
            CourseSeries(
                summary=f"课{i}",
                dtstart=datetime.combine(
                    monday, dt_time(h, 0), tzinfo=SHANGHAI_TZ
                ),
                duration=timedelta(minutes=95),
                rrule_text="FREQ=DAILY",
            )
            for i, h in enumerate((8, 10, 14, 16))
        ]
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        plugin._load_series = lambda b: courses

        data = plugin._collect_study_rank(
            list(plugin._storage.load_bindings().values()),
            datetime.now(timezone.utc),
            "周",
        )
        assert data["rows"][0]["total"] == "44 小时 20 分"
        assert "天" not in data["rows"][0]["total"]


class TestAvatarProfile:
    """qq_official 头像/昵称:payload 提取 + 绑定持久化 + 渲染优先级。"""

    def test_extract_avatar_from_official_raw_payload(self):
        # qq_official:AstrBot 把原始 payload patch 进 raw_message.raw_data
        event = FakeEvent(
            "openid-abc",
            raw_author={"avatar": "https://thirdqq.qq.com/avatar.png"},
        )
        assert _avatar_from_event(event) == "https://thirdqq.qq.com/avatar.png"

    def test_extract_falls_back_to_qlogo_for_numeric_id(self):
        # aiocqhttp:user_id 即 QQ 号,payload 里没有 avatar 也推导得出
        event = FakeEvent("123456")
        assert _avatar_from_event(event) == _avatar_url("123456")

    def test_extract_returns_empty_for_openid_without_payload(self):
        # qq_official 未下发 avatar 时返回空串(模板 onerror 隐藏兜底)
        event = FakeEvent("openid-abc")
        assert _avatar_from_event(event) == ""

    def test_avatar_for_prefers_recorded_avatar(self):
        b = make_binding("openid-abc", avatar="https://thirdqq.qq.com/a.png")
        assert _avatar_for(b) == "https://thirdqq.qq.com/a.png"

    def test_avatar_for_falls_back_to_qlogo(self):
        assert _avatar_for(make_binding("123456")) == _avatar_url("123456")

    def test_upsert_persists_avatar_and_keeps_previous(self, storage):
        b = storage.upsert_binding(
            user_id="op1",
            unified_msg_origin="t",
            nickname="n",
            avatar="https://thirdqq.qq.com/old.png",
        )
        assert b.avatar == "https://thirdqq.qq.com/old.png"
        # 重新绑定但 payload 没带头像 → 保留旧头像
        b2 = storage.upsert_binding(
            user_id="op1", unified_msg_origin="t2", nickname="n2"
        )
        assert b2.avatar == "https://thirdqq.qq.com/old.png"
        # payload 带新头像 → 覆盖更新
        b3 = storage.upsert_binding(
            user_id="op1",
            unified_msg_origin="t3",
            nickname="n3",
            avatar="https://thirdqq.qq.com/new.png",
        )
        assert b3.avatar == "https://thirdqq.qq.com/new.png"


class TestLinkBinding:
    """/关联课表:昵称+头像匹配候选、确认交互前置分支与复用逻辑。"""

    OFFICIAL_AVATAR = "https://thirdqq.qq.com/me.png"

    @staticmethod
    def _b(uid, nickname, avatar=""):
        b = make_binding(uid, avatar=avatar)
        b.nickname = nickname
        return b

    def _official_event(self, uid="me"):
        # 模拟 qq_official:原始 payload 的 author 带头像
        return FakeEvent(uid, raw_author={"avatar": self.OFFICIAL_AVATAR})

    @staticmethod
    def _run(plugin, event):
        async def run():
            return [r async for r in plugin.link(event)]

        return asyncio.run(run())

    # ---- _find_link_candidates ----

    def test_candidates_strong_nickname_and_avatar(self):
        bindings = {
            "a": self._b("a", "Alice", avatar=self.OFFICIAL_AVATAR),
            "b": self._b("b", "Bob"),
        }
        strong, weak = _find_link_candidates(
            bindings, "me", "Alice", self.OFFICIAL_AVATAR
        )
        assert [b.user_id for b in strong] == ["a"]
        assert weak == []

    def test_candidates_weak_when_avatar_differs(self):
        # 跨平台头像地址不同(qlogo vs 官方 CDN)→ 仅昵称匹配,落入弱候选
        bindings = {"a": self._b("a", "Alice", avatar=_avatar_url("a"))}
        strong, weak = _find_link_candidates(
            bindings, "me", "Alice", self.OFFICIAL_AVATAR
        )
        assert strong == []
        assert [b.user_id for b in weak] == ["a"]

    def test_candidates_exclude_self_and_blank_nicknames(self):
        bindings = {
            "me": self._b("me", "Alice", avatar=self.OFFICIAL_AVATAR),
            "x": self._b("x", "   "),  # 空白昵称不参与
        }
        strong, weak = _find_link_candidates(
            bindings, "me", "Alice", self.OFFICIAL_AVATAR
        )
        assert strong == []
        assert weak == []

    def test_candidates_require_nickname_and_avatar(self):
        assert _find_link_candidates({}, "me", "", "http://a") == ([], [])
        assert _find_link_candidates({}, "me", "Alice", "") == ([], [])

    # ---- link 命令前置分支 ----

    def test_link_hints_when_already_bound(self, plugin):
        plugin._storage.save_bindings({"me": self._b("me", "tester")})
        results = self._run(plugin, self._official_event())
        assert results == ["你已绑定课表。如需更换，请先使用 /删除课表。"]

    def test_link_hints_direct_bind_without_match(self, plugin):
        results = self._run(plugin, self._official_event())
        assert "请直接使用 /绑定课表" in results[0]

    def test_link_hints_when_nickname_missing(self, plugin):
        # 官方未下发昵称 → 无法可靠匹配
        event = self._official_event()
        event.get_sender_name = lambda: ""
        results = self._run(plugin, event)
        assert "无法匹配" in results[0]

    def test_link_asks_confirmation_on_unique_strong_match(self, plugin):
        plugin._storage.save_bindings(
            {"a": self._b("a", "tester", avatar=self.OFFICIAL_AVATAR)}
        )
        results = self._run(plugin, self._official_event())
        assert "找到昵称为「tester」的已绑定课表" in results[0]
        assert "确认" in results[0] and "退出" in results[0]

    def test_link_weak_match_notes_verification(self, plugin):
        plugin._storage.save_bindings(
            {"a": self._b("a", "tester", avatar="https://other/cdn.png")}
        )
        results = self._run(plugin, self._official_event())
        assert "请自行确认" in results[0]

    def test_link_hints_direct_bind_on_multiple_matches(self, plugin):
        plugin._storage.save_bindings(
            {
                "a": self._b("a", "tester", avatar=self.OFFICIAL_AVATAR),
                "b": self._b("b", "tester", avatar=self.OFFICIAL_AVATAR),
            }
        )
        results = self._run(plugin, self._official_event())
        assert "请直接使用 /绑定课表" in results[0]

    # ---- _apply_link 复用执行 ----

    def test_apply_link_copies_ics_and_binds(self, plugin, monkeypatch):
        target = self._b("a", "tester", avatar=self.OFFICIAL_AVATAR)
        src = plugin._storage.get_ics_path("a")
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("BEGIN:VCALENDAR", encoding="utf-8")
        monkeypatch.setattr(
            plugin._parser, "parse_ics_file", lambda p: [object(), object()]
        )

        event = self._official_event()
        message = plugin._apply_link("me", event, target)

        assert "关联成功" in message and "2 条" in message
        binding = plugin._storage.get_binding("me")
        assert binding is not None
        assert binding.unified_msg_origin == event.unified_msg_origin
        assert binding.avatar == self.OFFICIAL_AVATAR  # 记录自己的头像
        dst = plugin._storage.resolve_ics_path(binding)
        assert dst.exists() and dst != src  # 复制而非引用

    def test_apply_link_fails_when_source_missing(self, plugin):
        message = plugin._apply_link(
            "me", self._official_event(), self._b("ghost", "tester")
        )
        assert "关联失败" in message

    def test_apply_link_fails_on_parse_error(self, plugin, monkeypatch):
        target = self._b("a", "tester")
        src = plugin._storage.get_ics_path("a")
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("garbage", encoding="utf-8")
        monkeypatch.setattr(plugin._parser, "parse_ics_file", lambda p: None)

        message = plugin._apply_link("me", self._official_event(), target)

        assert "关联失败" in message
        assert not plugin._storage.get_ics_path("me").exists()  # 失败时删除复制的文件


class TestProfileRefresh:
    """OneBot 查询时自动刷新群名片/昵称(写回)与群聚合图的实时名片。"""

    class _FakeBot:
        """模拟 OneBot client:call_action 返回预设或抛错,记录调用。"""

        def __init__(self, responses=None, error=None):
            self.responses = responses or {}
            self.error = error
            self.calls = []

        async def call_action(self, action=None, **kwargs):
            self.calls.append((action, kwargs))
            if self.error:
                raise self.error
            return self.responses[action]

        # 兼容旧版 aiocqhttp 的调用入口名
        call_api = call_action

    @staticmethod
    def _b(uid, nickname):
        b = make_binding(uid)
        b.nickname = nickname
        return b

    def _group_event(self, uid="1", bot=None):
        event = FakeEvent(uid)
        event.message_obj.group_id = "123456"
        if bot is not None:
            event.bot = bot
        return event

    def test_refresh_updates_nickname_from_group_card(self, plugin):
        plugin._storage.save_bindings({"1": self._b("1", "旧名片")})
        binding = plugin._storage.get_binding("1")
        bot = self._FakeBot(
            {"get_group_member_info": {"card": "新名片", "nickname": "QQ昵称"}}
        )

        asyncio.run(plugin._refresh_binding_profile(self._group_event(bot=bot), binding))

        assert plugin._storage.get_binding("1").nickname == "新名片"
        assert binding.nickname == "新名片"  # 传入对象同步更新
        assert plugin._storage.get_binding("1").avatar == _avatar_url("1")
        # 只调了群成员信息 API(名片非空无需兜底查询)
        assert [c[0] for c in bot.calls] == ["get_group_member_info"]

    def test_refresh_works_with_custom_platform_name(self, plugin):
        """平台适配器名可自定义(如 onebot/napcat),不应影响刷新。"""
        plugin._storage.save_bindings({"1": self._b("1", "旧名片")})
        binding = plugin._storage.get_binding("1")
        bot = self._FakeBot(
            {"get_group_member_info": {"card": "新名片", "nickname": "n"}}
        )
        event = self._group_event(bot=bot)
        event.get_platform_name = lambda: "onebot"

        asyncio.run(plugin._refresh_binding_profile(event, binding))

        assert plugin._storage.get_binding("1").nickname == "新名片"

    def test_refresh_falls_back_to_stranger_info_when_card_empty(self, plugin):
        """群名片为空(未设置群名片)时补查 QQ 昵称。"""
        plugin._storage.save_bindings({"1": self._b("1", "旧名片")})
        binding = plugin._storage.get_binding("1")
        bot = self._FakeBot(
            {
                "get_group_member_info": {"card": "", "nickname": "x"},
                "get_stranger_info": {"nick": "QQ昵称"},
            }
        )

        asyncio.run(plugin._refresh_binding_profile(self._group_event(bot=bot), binding))

        assert plugin._storage.get_binding("1").nickname == "QQ昵称"

    def test_refresh_keeps_old_nickname_on_api_failure(self, plugin):
        plugin._storage.save_bindings({"1": self._b("1", "旧名片")})
        binding = plugin._storage.get_binding("1")
        bot = self._FakeBot(error=RuntimeError("api down"))

        asyncio.run(plugin._refresh_binding_profile(self._group_event(bot=bot), binding))

        assert plugin._storage.get_binding("1").nickname == "旧名片"

    def test_refresh_private_chat_uses_stranger_info(self, plugin):
        plugin._storage.save_bindings({"1": self._b("1", "旧名片")})
        binding = plugin._storage.get_binding("1")
        event = FakeEvent("1")  # 无 group_id → 私聊分支
        bot = self._FakeBot({"get_stranger_info": {"nickname": "私聊昵称"}})
        event.bot = bot

        asyncio.run(plugin._refresh_binding_profile(event, binding))

        assert plugin._storage.get_binding("1").nickname == "私聊昵称"

    def test_refresh_skipped_without_bot(self, plugin):
        plugin._storage.save_bindings({"1": self._b("1", "旧名片")})
        binding = plugin._storage.get_binding("1")

        asyncio.run(plugin._refresh_binding_profile(self._group_event(), binding))

        assert plugin._storage.get_binding("1").nickname == "旧名片"

    def test_group_cards_fetched_for_aggregate_views(self, plugin):
        bot = self._FakeBot(
            {
                "get_group_member_list": [
                    {"user_id": 1, "card": "新名片A", "nickname": "昵称A"},
                    {"user_id": 2, "card": "", "nickname": "昵称B"},
                ]
            }
        )
        event = self._group_event(bot=bot)

        cards = asyncio.run(plugin._fetch_group_cards(event))

        assert cards == {"1": "新名片A", "2": "昵称B"}

    def test_fetch_group_cards_skipped_without_group(self, plugin):
        assert asyncio.run(plugin._fetch_group_cards(FakeEvent("1"))) == {}

    def test_collect_group_now_uses_fresh_cards(self, plugin):
        today = datetime.now(SHANGHAI_TZ).date()
        start = datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        series = [
            CourseSeries(
                summary="高等数学",
                dtstart=start,
                duration=timedelta(minutes=95),
                location="明理楼302",
            )
        ]
        plugin._load_series = lambda b: series
        members = [self._b("1", "旧名片")]
        now_utc = (start + timedelta(minutes=30)).astimezone(timezone.utc)

        data = plugin._collect_group_now(members, now_utc, cards={"1": "群新名片"})
        assert data["groups"][0]["members"][0]["nickname"] == "群新名片"

        data2 = plugin._collect_group_now([members[0]], now_utc)
        assert data2["groups"][0]["members"][0]["nickname"] == "旧名片"  # 无 cards 回落

    def test_collect_study_rank_uses_fresh_cards(self, plugin):
        today = datetime.now(SHANGHAI_TZ).date()
        start = datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ)
        series = [
            CourseSeries(
                summary="高等数学",
                dtstart=start,
                duration=timedelta(minutes=95),
                location="明理楼302",
            )
        ]
        plugin._load_series = lambda b: series
        members = [self._b("1", "旧名片")]

        data = plugin._collect_study_rank(
            members, start.astimezone(timezone.utc), "日", cards={"1": "群新名片"}
        )
        assert data["rows"][0]["nickname"] == "群新名片"


class TestAtQuery:
    """查询命令支持命令后 at 群友,查看 TA 的课表。"""

    @staticmethod
    def _binding(uid, nickname):
        b = make_binding(uid)
        b.nickname = nickname
        return b

    @staticmethod
    def _today_courses():
        from datetime import time as dt_time

        today = datetime.now(SHANGHAI_TZ).date()
        return [
            CourseSeries(
                summary="高等数学",
                dtstart=datetime.combine(today, dt_time(9, 0), tzinfo=SHANGHAI_TZ),
                duration=timedelta(minutes=95),
                location="明理楼302",
            )
        ]

    def test_extract_at_target(self, plugin):
        assert plugin._extract_at_target(FakeEvent("x", at="u1")) == "u1"
        assert plugin._extract_at_target(FakeEvent("x")) is None
        assert plugin._extract_at_target(FakeEvent("x", at="all")) is None  # @全体不算

    def test_at_other_shows_their_day_schedule(self, plugin):
        plugin._storage.save_bindings(
            {"u1": self._binding("u1", "Alice"), "u2": self._binding("u2", "Bob")}
        )
        # Alice 有课,Bob 没有
        plugin._load_series = (
            lambda b: self._today_courses() if b.user_id == "u1" else []
        )
        captured = []

        async def fake_render(template, data, options=None):
            captured.append(data)
            return "http://fake/at_day.png"

        plugin.html_render = fake_render

        async def run():
            return [r async for r in plugin.today(FakeEvent("x", at="u1"))]

        results = asyncio.run(run())
        assert results == [("image", "http://fake/at_day.png")]
        # 展示的是被 at 的 Alice 的课表(副标题带她的昵称)
        assert "Alice" in captured[0]["subtitle"]
        assert "Bob" not in captured[0]["subtitle"]
        # 头部带用户头像与名字
        assert captured[0]["avatar"] == _avatar_url("u1")

    def test_at_query_uses_recorded_avatar(self, plugin):
        """qq_official:绑定记录的头像优先于 qlogo 推导(openid 推导无效)。"""
        alice = self._binding("openid-xyz", "Alice")
        alice.avatar = "https://thirdqq.qq.com/alice.png"
        plugin._storage.save_bindings({"openid-xyz": alice})
        plugin._load_series = lambda b: self._today_courses()
        captured = []

        async def fake_render(template, data, options=None):
            captured.append(data)
            return "http://fake/official.png"

        plugin.html_render = fake_render

        async def run():
            return [r async for r in plugin.today(FakeEvent("x", at="openid-xyz"))]

        asyncio.run(run())
        assert captured[0]["avatar"] == "https://thirdqq.qq.com/alice.png"

    def test_at_unbound_user_hints(self, plugin):
        plugin._storage.save_bindings({"u2": self._binding("u2", "Bob")})

        async def run():
            return [r async for r in plugin.today(FakeEvent("x", at="ghost"))]

        results = asyncio.run(run())
        assert results == ["TA 还没有绑定课表。"]

    def test_at_all_falls_back_to_self(self, plugin):
        """@全体成员不算 at 他人:退回查询自己的课表。"""
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})

        async def run():
            return [r async for r in plugin.today(FakeEvent("x", at="all"))]

        results = asyncio.run(run())
        assert results == ["你还没有绑定课表。请先使用 /绑定课表"]

    def test_no_at_queries_self(self, plugin):
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        courses = self._today_courses()
        plugin._load_series = lambda b: courses if b.user_id == "u1" else []
        captured = []

        async def fake_render(template, data, options=None):
            captured.append(data)
            return "http://fake/self_day.png"

        plugin.html_render = fake_render

        async def run():
            return [r async for r in plugin.today(FakeEvent("u1"))]

        results = asyncio.run(run())
        assert results == [("image", "http://fake/self_day.png")]
        assert "Alice" in captured[0]["subtitle"]  # 查自己

    def test_week_supports_at_other(self, plugin):
        plugin._storage.save_bindings(
            {"u1": self._binding("u1", "Alice"), "u2": self._binding("u2", "Bob")}
        )
        plugin._load_series = (
            lambda b: self._today_courses() if b.user_id == "u1" else []
        )
        captured = []

        async def fake_render(template, data, options=None):
            captured.append(data)
            return "http://fake/at_week.png"

        plugin.html_render = fake_render

        async def run():
            return [r async for r in plugin.week(FakeEvent("x", at="u1"))]

        results = asyncio.run(run())
        assert results == [("image", "http://fake/at_week.png")]
        assert "Alice" in captured[0]["subtitle"]
        assert captured[0]["avatar"] == _avatar_url("u1")  # 周课表同样带头像

    def test_at_other_with_broken_series_hints_rebind(self, plugin):
        plugin._storage.save_bindings({"u1": self._binding("u1", "Alice")})
        plugin._load_series = lambda b: None

        async def run():
            return [r async for r in plugin.today(FakeEvent("x", at="u1"))]

        results = asyncio.run(run())
        assert "课表文件读取或解析失败" in results[0]

