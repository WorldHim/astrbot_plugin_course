from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence, Set

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.io import download_file
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .course_types import CourseEvent, CourseSeries, SHANGHAI_TZ, UserBinding
from .help_content import TOOL_LINK, help_render_data, help_text
from .ics_parser import IcsParser
from .render_templates import DAY_TMPL, GROUP_TMPL, HELP_TMPL, RANK_TMPL, WEEK_TMPL
from .schedule_engine import (
    class_time_in_window,
    current_or_next_event,
    day_events,
    upcoming_within_15m,
    week_events,
    week_start,
)
from .storage import CourseStorage


class _SenderSessionFilter(SessionFilter):
    """按 (会话, 发送者) 界定多轮会话。

    默认的 DefaultSessionFilter 只按 unified_msg_origin(群聊=群 ID)界定,
    群聊中会话等待期间【其他成员】的消息也会被当作当前用户的输入——
    例如 A 绑定课表时,群里 B 发的文件会被绑到 A 名下。加上发送者 ID
    后,只有发起指令的用户本人的后续消息才会进入会话。
    """

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

# 插件版本(@register 与帮助图片共用;metadata.yaml 的 version 需保持一致)
PLUGIN_VERSION = "2.6.2"

# 星期几的中文标签(周课表与上课时长榜共用)
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@register(
    "astrbot_plugin_course",
    "WorldHim",
    "绑定个人课表，查看今日/明日/本周/下周课表，并支持开课前发送课程提醒，以及每日定时发送课表。",
    PLUGIN_VERSION,
)
class CoursePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._context = context
        # AstrBot 会在存在 _conf_schema.json 时注入插件配置(AstrBotConfig,dict 子类)
        self._config = config

        self._storage = CourseStorage(self.name)
        self._parser = IcsParser()

        self._reminded: Dict[str, Set[str]] = {}
        self._stop_event = asyncio.Event()
        self._reminder_task: Optional[asyncio.Task[None]] = None
        self._initializing_lock = asyncio.Lock()
        # 渲染结果缓存:内容 hash -> (过期时间戳, 图片 URL)
        self._render_cache: Dict[str, tuple[float, str]] = {}

    def _cfg(self, key: str, default):
        """读取插件配置(WebUI 可调);未注入配置时使用默认值。"""
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_int(self, key: str, default: int, minimum: int = 1) -> int:
        """读取整型插件配置;非法或低于下限时回退默认值。"""
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= minimum else default

    async def initialize(self):
        # 使用锁防止并发初始化
        async with self._initializing_lock:
            logger.info("[course] initializing reminder system...")
            
            # 检查并安全取消旧任务
            if self._reminder_task is not None:
                if not self._reminder_task.done():
                    logger.info("[course] cancelling old reminder task...")
                    self._stop_event.set()
                    self._reminder_task.cancel()
                    try:
                        # 等待任务完全终止（超时 5 秒）
                        await asyncio.wait_for(self._reminder_task, timeout=5)
                    except asyncio.CancelledError:
                        logger.info("[course] old reminder task cancelled successfully")
                    except asyncio.TimeoutError:
                        logger.warning("[course] old reminder task did not terminate within timeout")
                    except Exception as e:
                        logger.warning(f"[course] unexpected error waiting for old task: {e}")
                self._reminder_task = None
            
            # 恢复提醒记录(持久化),避免重启/重载后重复提醒
            self._reminded = self._storage.load_reminded()
            self._stop_event.clear()  # 清除停止信号
            logger.info("[course] reminder state restored")
            
            # 创建新任务
            self._reminder_task = asyncio.create_task(self._reminder_loop())
            logger.info("[course] new reminder task created")

        # 注册用户的每日推送任务（在锁外执行，避免阻塞；并发注册，启动耗时与用户数无关）
        bindings = self._storage.load_bindings()
        tasks = [
            self._register_user_cron(user_id, binding.daily_push_time)
            for user_id, binding in bindings.items()
            if binding.enable_daily_push and binding.daily_push_time
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"[course] failed to register cron: {result!r}")

    async def terminate(self):
        logger.info("[course] terminating reminder system...")
        
        # 设置停止信号
        self._stop_event.set()
        
        # 安全取消任务并等待其完全终止
        if self._reminder_task is not None:
            if not self._reminder_task.done():
                logger.info("[course] cancelling reminder task...")
                self._reminder_task.cancel()
                try:
                    # 等待任务完全终止（超时 5 秒）
                    await asyncio.wait_for(self._reminder_task, timeout=5)
                except asyncio.CancelledError:
                    logger.info("[course] reminder task cancelled successfully")
                except asyncio.TimeoutError:
                    logger.warning("[course] reminder task did not terminate within timeout")
                except Exception as e:
                    logger.warning(f"[course] unexpected error during task termination: {e}")
            self._reminder_task = None
        
        # 清空提醒状态
        self._reminded.clear()
        
        logger.info("[course] reminder system terminated")

    @filter.command("绑定课表")
    async def bind(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        nickname = str(event.get_sender_name())
        wait_seconds = self._cfg_int("bind_wait_seconds", 120, 10)

        yield event.plain_result(
            f"请在 {wait_seconds} 秒内发送 .ics 文件。\n"
            "发送\"退出\"可取消。"
        )

        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = (evt.message_str or "").strip()
            if text == "退出":
                await evt.send(evt.plain_result("已取消绑定。"))
                controller.stop()
                return

            ics_path = self._storage.get_ics_path(user_id)

            file_source = await _try_get_file_source(evt)
            if file_source is not None:
                kind, source = file_source
                await evt.send(evt.plain_result("正在接收课表..."))
                try:
                    if kind == "url":
                        await download_file(source, str(ics_path))
                    else:
                        shutil.copyfile(source, str(ics_path))
                except Exception as e:
                    logger.error(f"[course] save ics failed ({kind}): {e}")
                    await evt.send(evt.plain_result("文件保存失败，请重试。"))
                    controller.stop()
                    return

                # 保存后立即校验，避免把无效文件绑定成课表
                try:
                    ics_size = ics_path.stat().st_size
                except OSError:
                    ics_size = 0
                max_ics_bytes = self._cfg_int("max_ics_mb", 5, 1) * 1024 * 1024
                if ics_size <= 0 or ics_size > max_ics_bytes:
                    ics_path.unlink(missing_ok=True)
                    await evt.send(
                        evt.plain_result("收到的文件不是有效的课表，绑定失败，请重新上传。")
                    )
                    controller.stop()
                    return

                parsed = self._parser.parse_ics_file(str(ics_path))
                if parsed is None:
                    ics_path.unlink(missing_ok=True)
                    await evt.send(
                        evt.plain_result(
                            "课表解析失败，请确认上传的是 .ics 课表文件后重新发送。"
                        )
                    )
                    controller.stop()
                    return
                if not parsed:
                    ics_path.unlink(missing_ok=True)
                    await evt.send(
                        evt.plain_result(
                            "文件中未识别到任何课程，请确认导出的是课表 ics 后重新发送。"
                        )
                    )
                    controller.stop()
                    return

                self._storage.upsert_binding(
                    user_id=user_id,
                    unified_msg_origin=evt.unified_msg_origin,
                    nickname=nickname,
                    default_reminder_enabled=bool(
                        self._cfg("default_reminder_enabled", False)
                    ),
                    default_reminder_minutes=self._cfg_int(
                        "default_reminder_minutes", 15, 1
                    ),
                    default_push_time=self._cfg("default_push_time", "07:00"),
                    default_timezone=self._cfg("default_timezone", "Asia/Shanghai"),
                )
                await evt.send(
                    evt.plain_result(f"绑定成功，共识别到 {len(parsed)} 条课程安排。")
                )
                controller.stop()
                return

            controller.keep(timeout=wait_seconds, reset_timeout=True)

        waiter = session_waiter(
            timeout=wait_seconds, record_history_chains=False
        )(waiter)
        try:
            await waiter(event, session_filter=_SenderSessionFilter())
        except TimeoutError:
            yield event.plain_result("绑定超时。")
        finally:
            event.stop_event()

    @filter.command("删除课表")
    async def delete(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())

        await self._unregister_user_cron(user_id)

        binding = self._storage.get_binding(user_id)
        ok = self._storage.delete_binding(user_id)
        if ok:
            # 顺带清除该课表的解析缓存，避免已删除文件的缓存条目常驻内存
            if binding:
                ics_path = self._storage.resolve_ics_path(binding)
                self._parser.clear_cache(str(ics_path))
            self._reminded.pop(user_id, None)
            self._storage.save_reminded(self._reminded)
            yield event.plain_result("已删除课表。")
        else:
            yield event.plain_result("你还没有绑定课表。")

    @filter.command("设置每日推送")
    async def set_daily_push(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        binding = self._storage.get_binding(user_id)
        if not binding:
            yield event.plain_result("你还没有绑定课表。请先使用 /绑定课表")
            return

        wait_seconds = self._cfg_int("bind_wait_seconds", 120, 10)
        yield event.plain_result(
            "请回复以下格式设置每日推送：\n"
            "开启 HH:MM （例如：开启 07:00）\n"
            '或回复"关闭"禁用每日推送\n'
            f'{wait_seconds}秒内有效，发送"退出"可取消。'
        )

        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = (evt.message_str or "").strip()
            if text == "退出":
                await evt.send(evt.plain_result("已取消设置。"))
                controller.stop()
                return

            if text == "关闭":
                bindings = self._storage.load_bindings()
                if user_id in bindings:
                    bindings[user_id].enable_daily_push = False
                    self._storage.save_bindings(bindings)
                await self._unregister_user_cron(user_id)
                await evt.send(evt.plain_result("已关闭每日推送。"))
                controller.stop()
                return

            parts = text.split()
            if len(parts) == 2 and parts[0] == "开启":
                # 兼容帮助文案中的全角冒号(如“07：00”),统一归一化为半角再校验/存储
                time_str = parts[1].replace("：", ":")
                if not _is_valid_time_format(time_str):
                    controller.keep(timeout=wait_seconds, reset_timeout=True)
                    return

                bindings = self._storage.load_bindings()
                if user_id in bindings:
                    bindings[user_id].enable_daily_push = True
                    bindings[user_id].daily_push_time = time_str
                    self._storage.save_bindings(bindings)

                await self._unregister_user_cron(user_id)
                await self._register_user_cron(user_id, time_str)

                await evt.send(
                    evt.plain_result(f"已开启每日推送，推送时间：{time_str}")
                )
                controller.stop()
                return

            controller.keep(timeout=wait_seconds, reset_timeout=True)

        waiter = session_waiter(
            timeout=wait_seconds, record_history_chains=False
        )(waiter)
        try:
            await waiter(event, session_filter=_SenderSessionFilter())
        except TimeoutError:
            yield event.plain_result("设置超时。")
        finally:
            event.stop_event()

    @filter.command("设置提醒时间")
    async def set_reminder_time(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        binding = self._storage.get_binding(user_id)
        if not binding:
            yield event.plain_result("你还没有绑定课表。请先使用 /绑定课表")
            return

        wait_seconds = self._cfg_int("bind_wait_seconds", 120, 10)
        yield event.plain_result(
            "请回复提前提醒的分钟数（例如：15 表示提前15分钟，即可开启提醒）\n"
            f'回复"关闭"可关闭开课提醒。{wait_seconds}秒内有效，发送"退出"可取消。'
        )

        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = (evt.message_str or "").strip()
            if text == "退出":
                await evt.send(evt.plain_result("已取消设置。"))
                controller.stop()
                return

            if text == "关闭":
                bindings = self._storage.load_bindings()
                if user_id in bindings:
                    bindings[user_id].enable_reminder = False
                    self._storage.save_bindings(bindings)
                await evt.send(evt.plain_result("已关闭开课提醒。"))
                controller.stop()
                return

            try:
                minutes = int(text)
                if minutes < 1 or minutes > 120:
                    controller.keep(timeout=wait_seconds, reset_timeout=True)
                    return

                bindings = self._storage.load_bindings()
                if user_id in bindings:
                    bindings[user_id].enable_reminder = True
                    bindings[user_id].reminder_advance_minutes = minutes
                    self._storage.save_bindings(bindings)
                await evt.send(
                    evt.plain_result(f"已开启开课提醒：提前 {minutes} 分钟")
                )
                controller.stop()
            except ValueError:
                controller.keep(timeout=wait_seconds, reset_timeout=True)

        waiter = session_waiter(
            timeout=wait_seconds, record_history_chains=False
        )(waiter)
        try:
            await waiter(event, session_filter=_SenderSessionFilter())
        except TimeoutError:
            yield event.plain_result("设置超时。")
        finally:
            event.stop_event()

    @filter.command("查看设置")
    async def view_settings(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        binding = self._storage.get_binding(user_id)
        if not binding:
            yield event.plain_result("你还没有绑定课表。请先使用 /绑定课表")
            return

        push_status = "已开启" if binding.enable_daily_push else "已关闭"
        reminder_status = (
            f"已开启（提前 {binding.reminder_advance_minutes} 分钟）"
            if binding.enable_reminder
            else "已关闭"
        )
        settings_text = (
            f"当前设置：\n"
            f"时区：{binding.timezone_name}\n"
            f"每日推送：{push_status}\n"
            f"推送时间：{binding.daily_push_time}\n"
            f"开课提醒：{reminder_status}"
        )
        yield event.plain_result(settings_text)

    @filter.command("设置时区")
    async def set_timezone(self, event: AstrMessageEvent):
        """设置课表使用的时区(IANA 名称,如 Asia/Shanghai)。"""
        user_id = str(event.get_sender_id())
        binding = self._storage.get_binding(user_id)
        if not binding:
            yield event.plain_result("你还没有绑定课表。请先使用 /绑定课表")
            return

        raw = (event.message_str or "").strip()
        idx = raw.find("设置时区")
        arg = (raw[idx + len("设置时区"):] if idx >= 0 else raw).strip()
        if not arg:
            yield event.plain_result(
                "请在指令后带上 IANA 时区名，例如：\n"
                "/设置时区 Asia/Shanghai\n"
                "/设置时区 America/New_York"
            )
            return

        tz = _resolve_timezone(arg)
        if tz is None:
            yield event.plain_result(
                f"无法识别时区「{arg}」，请使用 IANA 名称，"
                "如 Asia/Shanghai、America/New_York、Europe/London。"
            )
            return

        bindings = self._storage.load_bindings()
        if user_id in bindings:
            bindings[user_id].timezone_name = arg
            self._storage.save_bindings(bindings)
        yield event.plain_result(f"时区已设置为：{arg}")

    @filter.command("课表帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        """输出本插件的全部指令说明(图片;渲染失败时退化为文字)及导出工具链接。"""
        url = await self._render_schedule(
            HELP_TMPL,
            help_render_data(PLUGIN_VERSION),
            options={"quality": self._cfg_int("render_quality", 100, 1)},
        )
        if url is None:
            yield event.plain_result(help_text())
        else:
            yield event.image_result(url)
        # 长网址单独一条文本消息发出,保证客户端中可点击
        yield event.plain_result(TOOL_LINK)

    @filter.command("今日课表")
    async def today(self, event: AstrMessageEvent):
        async for r in self._send_day_schedule(event, day_offset=0):
            yield r

    @filter.command("明日课表")
    async def tomorrow(self, event: AstrMessageEvent):
        async for r in self._send_day_schedule(event, day_offset=1):
            yield r

    @filter.command("本周课表")
    async def week(self, event: AstrMessageEvent):
        binding, is_other = self._resolve_view_binding(event)
        if not binding:
            yield event.plain_result(
                "TA 还没有绑定课表。"
                if is_other
                else "你还没有绑定课表。请先使用 /绑定课表"
            )
            return

        series = self._load_series(binding)
        if series is None:
            yield event.plain_result(
                "课表文件读取或解析失败，请重新使用 /绑定课表 上传课表。"
            )
            return

        user_tz = binding.get_timezone()
        now = datetime.now(user_tz)
        today_date = now.date()
        start = week_start(today_date)
        week_lists = week_events(series, start, user_tz)
        days = []
        for i in range(7):
            d = start + timedelta(days=i)
            day_list = week_lists[i]
            days.append(
                {
                    "label": _WEEKDAY_NAMES[i],
                    "date": d.strftime("%m-%d"),
                    "is_today": d == today_date,
                    "courses": [_event_view(e) for e in day_list],
                }
            )

        title = f"本周课表"
        subtitle = f"{binding.nickname} | {start.strftime('%Y-%m-%d')} ~ {(start + timedelta(days=6)).strftime('%Y-%m-%d')}"
        url = await self._render_schedule(
            WEEK_TMPL,
            {
                "title": title,
                "subtitle": subtitle,
                "avatar": _avatar_url(binding.user_id),
                "days": days,
                "page_width": self._cfg_int("week_render_width", 1280, 320),
            },
            options={"quality": self._cfg_int("render_quality", 100, 1)},
        )
        if url is None:
            # 图片渲染失败(如 Playwright 未安装),退化为文字课表
            yield event.plain_result(_week_text_fallback(title, subtitle, days))
            return
        yield event.image_result(url)

    @filter.command("当前课程")
    async def group_schedule(self, event: AstrMessageEvent):
        """看看本群现在都有谁在上课(头像+课程图片)。"""
        now_utc = datetime.now(timezone.utc)
        bindings = self._storage.load_bindings()
        members = [
            b
            for b in bindings.values()
            if b.unified_msg_origin == event.unified_msg_origin
        ]
        if not members:
            yield event.plain_result(
                "本群还没有人绑定课表。发送 /绑定课表 绑定后即可使用本指令。"
            )
            return

        data = self._collect_group_now(members, now_utc)
        url = await self._render_schedule(
            GROUP_TMPL,
            data,
            options={"quality": self._cfg_int("render_quality", 100, 1)},
        )
        if url is None:
            yield event.plain_result(_group_now_text(data))
            return
        yield event.image_result(url)

    def _collect_group_now(
        self, members: Sequence[UserBinding], now_utc: datetime
    ) -> dict:
        """收集群成员"此刻正在上"的课程并按课程分组(含头像)。

        返回渲染/文字兜底共用的数据结构;当前没在上课的成员不输出。
        groups: 同一时刻上同一门课的成员聚合为一组。
        """
        groups: Dict[tuple, dict] = {}
        for binding in members:
            series = self._load_series(binding)
            if series is None:
                continue
            user_tz = binding.get_timezone()
            current, _ = current_or_next_event(
                series, now_utc, user_tz, search_days=0
            )
            if current is None:
                continue

            nickname = binding.nickname or binding.user_id
            key = (current.summary, current.location, current.start_time.isoformat())
            group = groups.setdefault(
                key,
                {
                    "summary": current.summary,
                    "location": current.location or "",
                    "start": current.start_time,
                    "end": current.end_time,
                    "all_day": current.all_day,
                    "members": [],
                },
            )
            group["members"].append(
                {
                    "nickname": nickname,
                    "avatar": _avatar_url(binding.user_id),
                }
            )

        course_groups: List[dict] = []
        for g in sorted(groups.values(), key=lambda x: x["start"]):
            if g["all_day"]:
                time_part = "全天"
                remain = ""
            else:
                time_part = (
                    f"{g['start'].strftime('%H:%M')} - {g['end'].strftime('%H:%M')}"
                )
                remain = _format_in_minutes(
                    int((g["end"] - now_utc).total_seconds() // 60)
                )
            course_groups.append(
                {
                    "summary": g["summary"],
                    "location": g["location"],
                    "time": time_part,
                    "remain": remain,
                    "members": sorted(
                        g["members"], key=lambda m: m["nickname"]
                    ),
                }
            )
        return {
            "title": "本群正在上的课",
            "now": now_utc.astimezone(SHANGHAI_TZ).strftime("%m-%d %H:%M"),
            "groups": course_groups,
            "page_width": 480,
        }

    @filter.command("上课时长榜")
    async def study_rank(self, event: AstrMessageEvent):
        """看看本群谁今天上课最拼(图片)。"""
        async for r in self._send_study_rank(event, mode="日"):
            yield r

    @filter.command("上课时长周榜")
    async def study_week_rank(self, event: AstrMessageEvent):
        """看看本群谁这周上课最拼(图片)。"""
        async for r in self._send_study_rank(event, mode="周"):
            yield r

    async def _send_study_rank(self, event: AstrMessageEvent, mode: str):
        now_utc = datetime.now(timezone.utc)
        bindings = self._storage.load_bindings()
        members = [
            b
            for b in bindings.values()
            if b.unified_msg_origin == event.unified_msg_origin
        ]
        if not members:
            yield event.plain_result(
                "本群还没有人绑定课表。发送 /绑定课表 绑定后即可使用本指令。"
            )
            return

        data = self._collect_study_rank(members, now_utc, mode)
        url = await self._render_schedule(
            RANK_TMPL,
            data,
            options={"quality": self._cfg_int("render_quality", 100, 1)},
        )
        if url is None:
            yield event.plain_result(_study_rank_text(data))
            return
        yield event.image_result(url)

    def _collect_study_rank(
        self, members: Sequence[UserBinding], now_utc: datetime, mode: str
    ) -> dict:
        """统计本群成员的日/周上课总时长并排名(渲染与文字兜底共用)。

        mode: "日" 统计各自时区的今天,"周" 统计各自时区的本周(周一~周日)。
        每位成员按自己的时区取窗口(与个人课表查询口径一致);顶部展示的
        日期范围以东八区为准。0 时长的成员不上榜;并列时按昵称排序。
        """
        now_sh = now_utc.astimezone(SHANGHAI_TZ)
        if mode == "周":
            title = "本周上课时长榜"
            monday = week_start(now_sh.date())
            range_label = f"{monday:%m-%d} ~ {(monday + timedelta(days=6)):%m-%d}"
        else:
            title = "今日上课时长榜"
            range_label = f"{now_sh:%m-%d} {_WEEKDAY_NAMES[now_sh.weekday()]}"

        rows: list[dict] = []
        for binding in members:
            series = self._load_series(binding)
            if series is None:
                continue
            user_tz = binding.get_timezone()
            now_local = now_utc.astimezone(user_tz)
            if mode == "周":
                win_start = datetime.combine(
                    week_start(now_local.date()), dt_time.min, tzinfo=user_tz
                )
                win_end = win_start + timedelta(days=7)
            else:
                win_start = datetime.combine(
                    now_local.date(), dt_time.min, tzinfo=user_tz
                )
                win_end = win_start + timedelta(days=1)
            seconds, count = class_time_in_window(
                series, win_start, win_end, user_tz
            )
            if seconds <= 0:
                continue
            rows.append(
                {
                    "nickname": binding.nickname or binding.user_id,
                    "avatar": _avatar_url(binding.user_id),
                    "seconds": seconds,
                    "count": count,
                }
            )

        rows.sort(key=lambda r: (-r["seconds"], r["nickname"]))
        return {
            "title": title,
            "range": range_label,
            "rows": [
                {
                    "rank": i + 1,
                    "nickname": r["nickname"],
                    "avatar": r["avatar"],
                    "total": _format_rank_total(r["seconds"] // 60),
                    "count": r["count"],
                }
                for i, r in enumerate(rows)
            ],
            "page_width": 480,
        }

    @filter.command("下周课表")
    async def next_week(self, event: AstrMessageEvent):
        binding, is_other = self._resolve_view_binding(event)
        if not binding:
            yield event.plain_result(
                "TA 还没有绑定课表。"
                if is_other
                else "你还没有绑定课表。请先使用 /绑定课表"
            )
            return

        series = self._load_series(binding)
        if series is None:
            yield event.plain_result(
                "课表文件读取或解析失败，请重新使用 /绑定课表 上传课表。"
            )
            return

        user_tz = binding.get_timezone()
        now = datetime.now(user_tz)
        today_date = now.date()
        start = week_start(today_date) + timedelta(days=7)
        week_lists = week_events(series, start, user_tz)
        days = []
        labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for i in range(7):
            d = start + timedelta(days=i)
            day_list = week_lists[i]
            days.append(
                {
                    "label": labels[i],
                    "date": d.strftime("%m-%d"),
                    "is_today": False,
                    "courses": [_event_view(e) for e in day_list],
                }
            )

        title = f"下周课表"
        subtitle = f"{binding.nickname} | {start.strftime('%Y-%m-%d')} ~ {(start + timedelta(days=6)).strftime('%Y-%m-%d')}"
        url = await self._render_schedule(
            WEEK_TMPL,
            {
                "title": title,
                "subtitle": subtitle,
                "avatar": _avatar_url(binding.user_id),
                "days": days,
                "page_width": self._cfg_int("week_render_width", 1280, 320),
            },
            options={"quality": self._cfg_int("render_quality", 100, 1)},
        )
        if url is None:
            # 图片渲染失败(如 Playwright 未安装),退化为文字课表
            yield event.plain_result(_week_text_fallback(title, subtitle, days))
            return
        yield event.image_result(url)

    def _load_series(self, binding: UserBinding) -> Optional[list[CourseSeries]]:
        """加载并解析绑定用户的课表。

        返回 None 表示课表文件缺失或解析失败(区别于“没有课程”的空列表),
        调用方应向用户提示重新绑定。
        """
        ics_path = self._storage.resolve_ics_path(binding)
        series = self._parser.parse_ics_file(
            str(ics_path), default_tz=binding.get_timezone()
        )
        if series is None:
            logger.error(
                f"[course] failed to load ics for user {binding.user_id}: {ics_path}"
            )
        return series

    async def _render_schedule(self, template, data, options=None):
        """渲染课表图片;带内容 hash 缓存(可配置 TTL,0=禁用)。

        渲染失败(如 Playwright 未安装)时记录日志并返回 None,
        由调用方以文字课表兜底;失败结果不缓存。
        """
        cache_minutes = self._cfg_int("render_cache_minutes", 360, 0)
        now_ts = time.time()
        cache_key = hashlib.sha256(
            (template + "\x00" + json.dumps(data, ensure_ascii=False, sort_keys=True))
            .encode("utf-8")
        ).hexdigest()

        # viewport 高度取极小值:full_page 整页截图的高度 = max(内容高度, 视口高度),
        # 视口越小图片长度越贴近实际内容(默认 720px 会给内容少的页面留下大片空白)
        options = dict(options or {})
        options.setdefault("viewport_height", 8)

        entry = self._render_cache.get(cache_key)
        if cache_minutes > 0 and entry and entry[0] > now_ts:
            return entry[1]

        try:
            url = await self.html_render(template, data, options=options or {})
        except Exception as e:
            logger.error(f"[course] html render failed: {e}")
            return None

        if cache_minutes > 0 and url:
            if len(self._render_cache) >= 128:
                # 简单防膨胀:先清过期条目,仍超限则整体清空
                expired = [k for k, v in self._render_cache.items() if v[0] <= now_ts]
                for k in expired:
                    self._render_cache.pop(k, None)
                if len(self._render_cache) >= 128:
                    self._render_cache.clear()
            self._render_cache[cache_key] = (now_ts + cache_minutes * 60, url)
        return url

    @staticmethod
    def _extract_at_target(event: AstrMessageEvent) -> Optional[str]:
        """从消息中提取被 @ 的用户 ID;没有 at(@全体除外)返回 None。"""
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) != "all":
                return str(comp.qq)
        return None

    def _resolve_view_binding(self, event: AstrMessageEvent):
        """解析课表查询目标:命令后 at 了群友则查 TA 的课表。

        返回 (binding, is_other)。binding 为 None 时:
        is_other=False 表示自己未绑定,is_other=True 表示对方未绑定,
        调用方据此给出对应的提示文案。
        """
        target_id = self._extract_at_target(event)
        if target_id is None:
            return self._storage.get_binding(str(event.get_sender_id())), False
        return self._storage.get_binding(target_id), True

    async def _send_day_schedule(self, event: AstrMessageEvent, *, day_offset: int):
        binding, is_other = self._resolve_view_binding(event)
        if not binding:
            yield event.plain_result(
                "TA 还没有绑定课表。"
                if is_other
                else "你还没有绑定课表。请先使用 /绑定课表"
            )
            return

        series = self._load_series(binding)
        if series is None:
            yield event.plain_result(
                "课表文件读取或解析失败，请重新使用 /绑定课表 上传课表。"
            )
            return

        user_tz = binding.get_timezone()
        now = datetime.now(user_tz)
        target = now.date() + timedelta(days=day_offset)
        day_list = day_events(series, target, user_tz)

        title = "今日课表" if day_offset == 0 else "明日课表"
        subtitle = f"{binding.nickname} | {target.strftime('%Y-%m-%d')}"
        courses = [_event_view(e) for e in day_list]
        url = await self._render_schedule(
            DAY_TMPL,
            {
                "title": title,
                "subtitle": subtitle,
                "avatar": _avatar_url(binding.user_id),
                "courses": courses,
                "page_width": self._cfg_int("day_render_width", 500, 320),
            },
            options={"quality": self._cfg_int("render_quality", 100, 1)},
        )
        if url is None:
            # 图片渲染失败(如 Playwright 未安装),退化为文字课表
            yield event.plain_result(_day_text_fallback(title, subtitle, courses))
            return
        yield event.image_result(url)

    async def _register_user_cron(self, user_id: str, time_str: str) -> None:
        """为用户注册每日推送的 cron 任务"""
        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            logger.warning(f"[course] invalid time format for user {user_id}: {time_str}")
            return

        cron_expr = f"{minute} {hour} * * *"

        binding = self._storage.get_binding(user_id)
        if not binding:
            return

        old_job_ids = await self._collect_user_daily_job_ids(user_id, binding)
        for old_job_id in old_job_ids:
            try:
                await self._context.cron_manager.delete_job(old_job_id)
            except Exception as e:
                logger.debug(
                    f"[course] cleanup old cron job failed for {user_id}, job={old_job_id}: {e}"
                )

        payload = {
            "user_id": user_id,
            "unified_msg_origin": binding.unified_msg_origin,
            "nickname": binding.nickname,
            "ics_file": binding.ics_file,
        }

        try:
            job = await self._context.cron_manager.add_basic_job(
                name=f"每日课表推送_{user_id}",
                cron_expression=cron_expr,
                handler=self._daily_push_handler,
                description="每日课表推送",
                timezone="Asia/Shanghai",
                payload=payload,
                enabled=True,
                persistent=True,
            )
            bindings = self._storage.load_bindings()
            if user_id in bindings:
                bindings[user_id].daily_push_job_id = str(job.job_id)
                self._storage.save_bindings(bindings)
            logger.info(f"[course] registered cron job for user {user_id} at {time_str}")
        except Exception as e:
            logger.error(f"[course] failed to register cron job for user {user_id}: {e}")

    async def _unregister_user_cron(self, user_id: str) -> None:
        """取消用户的每日推送 cron 任务"""
        binding = self._storage.get_binding(user_id)
        job_ids = await self._collect_user_daily_job_ids(user_id, binding)
        for job_id in job_ids:
            try:
                await self._context.cron_manager.delete_job(job_id)
            except Exception as e:
                logger.debug(
                    f"[course] failed to unregister cron job for user {user_id}, job={job_id}: {e}"
                )

        bindings = self._storage.load_bindings()
        if user_id in bindings:
            bindings[user_id].daily_push_job_id = ""
            self._storage.save_bindings(bindings)

        logger.info(f"[course] unregistered cron jobs for user {user_id}: {len(job_ids)}")

    async def _daily_push_handler(self, **payload) -> None:
        """Cron 任务触发的每日推送处理函数"""
        user_id = payload.get("user_id")
        if not user_id:
            logger.warning("[course] daily push handler missing user_id")
            return

        binding = self._storage.get_binding(user_id)
        if not binding:
            logger.info(f"[course] user {user_id} binding not found, skipping daily push")
            return

        if not binding.enable_daily_push:
            logger.info(f"[course] user {user_id} daily push disabled, skipping")
            return

        try:
            series = self._load_series(binding)
            if series is None:
                logger.warning(
                    f"[course] daily push skipped: ics load failed for user {user_id}"
                )
                return
            user_tz = binding.get_timezone()
            now = datetime.now(user_tz)
            today = now.date()
            day_list = day_events(series, today, user_tz)

            title = "今日课表"
            subtitle = f"{binding.nickname} | {today.strftime('%Y-%m-%d')}"
            courses = [_event_view(e) for e in day_list]
            url = await self._render_schedule(
                DAY_TMPL,
                {
                    "title": title,
                    "subtitle": subtitle,
                    "courses": courses,
                    "page_width": self._cfg_int("day_render_width", 500, 320),
                },
                options={"quality": self._cfg_int("render_quality", 100, 1)},
            )
            session = MessageSession.from_str(binding.unified_msg_origin)
            if url is None:
                # 渲染失败,退化为文字课表,保证每日推送仍有内容
                text = _day_text_fallback(title, subtitle, courses)
                chain = MessageChain().message(text)
                await self._context.send_message(session, chain)
                logger.info(
                    f"[course] daily push sent as text for user {user_id} (render failed)"
                )
                return

            chain = MessageChain([Image.fromURL(url)])
            await self._context.send_message(session, chain)
            logger.info(f"[course] daily push sent to user {user_id}")
        except Exception as e:
            logger.error(f"[course] daily push failed for user {user_id}: {e}")

    async def _reminder_loop(self) -> None:
        tick_seconds = self._cfg_int("reminder_tick_seconds", 60, 5)
        while not self._stop_event.is_set():
            try:
                await self._tick_reminder()
            except Exception as e:
                logger.error(f"[course] reminder tick failed: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick_reminder(self) -> None:
        bindings = self._storage.load_bindings()
        if not bindings:
            return

        now_utc = datetime.now(timezone.utc)
        retention_days = self._cfg_int("reminded_retention_days", 30, 1)
        if self._cleanup_reminded(now_utc, retention_days):
            # 过期清理改变了记录,同步落盘
            self._storage.save_reminded(self._reminded)

        # 并发处理所有用户:单个用户发送缓慢/失败不影响其他用户
        send_timeout = self._cfg_int("reminder_send_timeout_seconds", 30, 5)
        results = await asyncio.gather(
            *(
                self._remind_user_with_timeout(user_id, binding, now_utc, send_timeout)
                for user_id, binding in bindings.items()
            ),
            return_exceptions=True,
        )
        # 任一用户有新增提醒记录(或超时导致状态不确定)时统一落盘一次
        need_save = False
        for result in results:
            if isinstance(result, BaseException):
                logger.error(f"[course] reminder task crashed: {result!r}")
                need_save = True
            elif result is True:
                need_save = True
        if need_save:
            self._storage.save_reminded(self._reminded)

    async def _remind_user_with_timeout(
        self, user_id: str, binding: UserBinding, now_utc: datetime, timeout: int
    ) -> bool:
        """带超时的单用户提醒;超时时记录已视为发送(防打扰优先),返回是否需落盘。"""
        try:
            return await asyncio.wait_for(
                self._remind_user(user_id, binding, now_utc), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[course] reminder for user {user_id} timed out after {timeout}s"
            )
            # 超时时提醒 key 可能已加入内存记录,同步落盘(防重启后重复提醒)
            return True
        except Exception as e:
            logger.error(f"[course] reminder task failed for user {user_id}: {e}")
            return False

    async def _remind_user(
        self, user_id: str, binding: UserBinding, now_utc: datetime
    ) -> bool:
        """处理单个用户的开课提醒;返回是否有新增提醒记录(需落盘)。"""
        try:
            if not binding.enable_reminder:
                # 用户未开启开课提醒(默认关闭):完全跳过
                return False
            user_tz = binding.get_timezone()
            now = now_utc.astimezone(user_tz)
            series = self._load_series(binding)
            if series is None:
                # 课表文件缺失或解析失败:跳过该用户的提醒(错误已由解析器记录)
                return False
            hits = upcoming_within_15m(
                now=now,
                user_id=user_id,
                events=series,
                advance_minutes=binding.reminder_advance_minutes,
                tz=user_tz,
            )
            if not hits:
                return False

            reminded = self._reminded.setdefault(user_id, set())
            new_keys: Set[str] = set()
            for hit in hits:
                key = hit.event.reminder_key()
                if key in reminded:
                    continue
                reminded.add(key)
                new_keys.add(key)
                msg = _reminder_text(hit.event, binding.reminder_advance_minutes)
                chain = MessageChain().message(msg)
                session = MessageSession.from_str(binding.unified_msg_origin)
                await self._context.send_message(session, chain)
            return bool(new_keys)
        except Exception as e:
            logger.error(f"[course] reminder failed for user {user_id}: {e}")
            return False

    async def _collect_user_daily_job_ids(
        self, user_id: str, binding=None
    ) -> Set[str]:
        job_ids: Set[str] = set()
        if binding and binding.daily_push_job_id:
            job_ids.add(binding.daily_push_job_id)

        try:
            jobs = await self._context.cron_manager.list_jobs("basic")
        except Exception as e:
            logger.debug(f"[course] list cron jobs failed: {e}")
            return job_ids

        for job in jobs:
            payload = getattr(job, "payload", {}) or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                continue
            if str(payload.get("user_id", "")) != user_id:
                continue
            name = str(getattr(job, "name", ""))
            description = str(getattr(job, "description", ""))
            if "每日课表推送" not in name and "每日课表推送" not in description:
                continue
            job_id = getattr(job, "job_id", "")
            if job_id:
                job_ids.add(str(job_id))
        return job_ids

    def _cleanup_reminded(self, now: datetime, retention_days: int = 30) -> bool:
        """清理过期的提醒记录;返回是否有变化(需同步落盘)。"""
        changed = False
        cutoff = now - timedelta(days=retention_days)
        for user_id in list(self._reminded.keys()):
            kept: Set[str] = set()
            for key in self._reminded[user_id]:
                start_time = _reminder_start_from_key(key)
                if start_time and start_time >= cutoff:
                    kept.add(key)
                else:
                    changed = True
            if kept:
                self._reminded[user_id] = kept
            else:
                self._reminded.pop(user_id, None)
        return changed


def _avatar_url(user_id: str) -> str:
    """QQ 头像 URL(qq 官方头像服务;模板 onerror 兜底隐藏)。"""
    return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"


def _courses_lines(courses) -> list[str]:
    """把课程 dict 列表格式化为文字行(渲染失败时的兜底展示)。"""
    lines = []
    for c in courses:
        loc = f" @{c.get('location')}" if c.get("location") else ""
        lines.append(f"{c['time_range']}  {c['summary']}{loc}")
    return lines


def _day_text_fallback(title: str, subtitle: str, courses) -> str:
    """日课表的文字版兜底(图片渲染失败时发给用户)。"""
    lines = [f"🖼️ 图片渲染失败，以下为文字版课表：", f"📅 {title}({subtitle})"]
    if not courses:
        lines.append("今日暂无课程，享受生活吧~")
    else:
        lines.extend(_courses_lines(courses))
    return "\n".join(lines)


def _week_text_fallback(title: str, subtitle: str, days) -> str:
    """周课表的文字版兜底(图片渲染失败时发给用户)。"""
    lines = [f"🖼️ 图片渲染失败，以下为文字版课表：", f"📅 {title}({subtitle})"]
    for day in days:
        day_lines = _courses_lines(day["courses"])
        head = f"【{day['label']} {day['date']}{' · 今天' if day.get('is_today') else ''}】"
        if day_lines:
            lines.append(head)
            lines.extend(day_lines)
        else:
            lines.append(f"{head} 无课")
    return "\n".join(lines)


def _resolve_timezone(name: str):
    """按 IANA 名称解析时区;无法识别返回 None。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def _course_label(e: CourseEvent) -> str:
    """课程速览用的一行标签:课程名(地点)。"""
    if e.location:
        return f"{e.summary}（{e.location}）"
    return e.summary


def _group_now_text(data: dict) -> str:
    """/当前课程 的文字版兜底(图片渲染失败时发送)。"""
    lines = [f"📋 本群正在上的课({data['now']})"]
    for g in data["groups"]:
        lines.append("")
        who = "、".join(m["nickname"] for m in g["members"])
        remain = f"（还剩 {g['remain']}）" if g["remain"] else ""
        loc = f"（{g['location']}）" if g["location"] else ""
        lines.append(f"🔴 {g['summary']}{loc} {g['time']}{remain}")
        lines.append(who)
    if not data["groups"]:
        lines.append("此刻本群没有成员正在上课。")
    return "\n".join(lines)


def _study_rank_text(data: dict) -> str:
    """/上课时长榜 的文字版兜底(图片渲染失败时发送)。"""
    lines = [f"🏆 {data['title']}({data['range']})"]
    medals = ("🥇", "🥈", "🥉")
    for r in data["rows"]:
        medal = (
            medals[r["rank"] - 1]
            if r["rank"] <= len(medals)
            else f"{r['rank']}."
        )
        lines.append(f"{medal} {r['nickname']}:{r['total']}(共 {r['count']} 节)")
    if len(lines) == 1:
        lines.append("本榜周期内暂无上课记录。")
    return "\n".join(lines)


def _format_in_minutes(total: int) -> str:
    """把分钟数转成人话:45 分钟 / 1 小时 30 分 / 2 天 3 小时。"""
    total = max(1, total)
    if total < 60:
        return f"{total} 分钟"
    hours, mins = divmod(total, 60)
    if hours < 24:
        return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    if hours:
        return f"{days} 天 {hours} 小时"
    return f"{days} 天"


def _format_rank_total(total: int) -> str:
    """上课时长榜的总时长格式:一律按小时结算,不进位到天。

    与 _format_in_minutes(剩余时间"还剩 X")不同——榜单里"2 天"没有
    直觉意义,统一显示为累计小时数:45 分钟 / 3 小时 10 分 / 26 小时。
    """
    total = max(1, total)
    if total < 60:
        return f"{total} 分钟"
    hours, mins = divmod(total, 60)
    if mins:
        return f"{hours} 小时 {mins} 分"
    return f"{hours} 小时"


def _event_view(e: CourseEvent) -> Dict[str, str]:
    if e.all_day:
        time_range = "全天"
    else:
        time_range = f"{e.start_time.strftime('%H:%M')} - {e.end_time.strftime('%H:%M')}"
    return {
        "summary": e.summary,
        "location": e.location or "",
        "time_range": time_range,
    }


def _reminder_text(e: CourseEvent, advance_minutes: int) -> str:
    loc = e.location.strip() if e.location else ""
    if loc:
        return f"开课提醒：{advance_minutes} 分钟后上课《{e.summary}》，地点：{loc}"
    return f"开课提醒：{advance_minutes} 分钟后上课《{e.summary}》"


def _reminder_start_from_key(key: str) -> Optional[datetime]:
    start_str = key.split("|", 1)[0].strip()
    if not start_str:
        return None
    try:
        dt = datetime.fromisoformat(start_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


async def _try_get_file_source(event: AstrMessageEvent) -> Optional[tuple[str, str]]:
    """从消息中提取用户发送的课表文件。

    返回 ("url", http_url) 或 ("local", 本地路径);未找到返回 None。
    兼容多种平台适配器行为:get_file() 返回协程或同步值、get_file 不支持
    allow_return_url 参数、组件上只有本地路径属性(file/path)等。
    """
    try:
        messages = event.get_messages()
    except Exception:
        return None

    for m in messages:
        try:
            if not (hasattr(m, "type") and getattr(m, "type") == "File"):
                continue

            candidate = None
            get_file = getattr(m, "get_file", None)
            if callable(get_file):
                try:
                    result = get_file(allow_return_url=True)
                except TypeError:
                    # 该适配器的 get_file 不接受 allow_return_url 参数
                    result = get_file()
                if asyncio.iscoroutine(result):
                    result = await result
                candidate = result

            if candidate is None:
                # 兜底:部分适配器直接在组件上挂文件路径属性
                candidate = getattr(m, "file", None) or getattr(m, "path", None)

            if not isinstance(candidate, str) or not candidate:
                continue
            if candidate.startswith("http"):
                return ("url", candidate)
            local = Path(candidate)
            if local.is_file():
                return ("local", str(local))
        except Exception as e:
            logger.debug(f"[course] extract file component failed: {e}")
            continue

    return None


def _is_valid_time_format(time_str: str) -> bool:
    import re

    return bool(re.match(r"^([0-1][0-9]|2[0-3]):[0-5][0-9]$", time_str))
