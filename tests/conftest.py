"""pytest 全局配置:以最小 stub 替代 astrbot 运行时,使插件模块可独立导入测试。

测试不触碰真实 data/plugin_data——storage 的数据目录由 fixture 重定向到 tmp_path。
"""
import logging
import sys
import types
from pathlib import Path

# tests/ -> 插件目录 -> data/plugins(包导入根)
PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent

# storage 数据目录占位,由 fixture 重定向到 tmp_path
_BASE = [str(Path(__file__).resolve().parent)]


def _install_astrbot_stubs():
    if "astrbot" in sys.modules:
        return

    fake_api = types.ModuleType("astrbot.api")
    fake_api.logger = logging.getLogger("astrbot-plugin-course-test")
    fake_astrbot = types.ModuleType("astrbot")
    fake_astrbot.api = fake_api

    fake_event_mod = types.ModuleType("astrbot.api.event")

    class _AstrMessageEvent:
        pass

    class _MessageChain:
        def __init__(self, *args, **kwargs):
            self.items = []
            for item in args:
                if isinstance(item, list):
                    self.items.extend(item)
                else:
                    self.items.append(item)

        def message(self, m):
            self.items.append(m)
            return self

    fake_event_mod.AstrMessageEvent = _AstrMessageEvent
    fake_event_mod.MessageChain = _MessageChain
    fake_event_mod.filter = types.SimpleNamespace(
        command=lambda *a, **k: (lambda f: f)
    )

    fake_components = types.ModuleType("astrbot.api.message_components")

    class _Image:
        def __init__(self, url):
            self.url = url

        @staticmethod
        def fromURL(u):
            return _Image(u)

    fake_components.Image = _Image

    fake_star_mod = types.ModuleType("astrbot.api.star")

    class _Context:
        pass

    class _Star:
        def __init__(self, context):
            self.context = context

    def _register(name, author, desc, ver):
        def deco(cls):
            cls.name = name
            return cls

        return deco

    fake_star_mod.Context = _Context
    fake_star_mod.Star = _Star
    fake_star_mod.register = _register
    fake_star_mod.StarTools = types.SimpleNamespace(
        get_data_dir=lambda n: Path(_BASE[0]) / n
    )

    fake_platform_mod = types.ModuleType("astrbot.core.platform.message_session")

    class _MessageSession:
        @staticmethod
        def from_str(s):
            return s

    fake_platform_mod.MessageSession = _MessageSession

    fake_io = types.ModuleType("astrbot.core.utils.io")

    async def _download_file(url, path):
        pass

    fake_io.download_file = _download_file

    fake_waiter = types.ModuleType("astrbot.core.utils.session_waiter")

    class _SessionController:
        pass

    class _SessionFilter:
        """与 astrbot 对应的会话过滤器基类 stub。"""

        def filter(self, event):
            return event.unified_msg_origin

    def _session_waiter(timeout=None, record_history_chains=False):
        def deco(f):
            async def wrapper(ev, session_filter=None, *a, **k):
                return await f(ev, session_filter, *a, **k)

            return wrapper

        return deco

        return deco

    fake_waiter.SessionController = _SessionController
    fake_waiter.SessionFilter = _SessionFilter
    fake_waiter.session_waiter = _session_waiter

    sys.modules["astrbot"] = fake_astrbot
    sys.modules["astrbot.api"] = fake_api
    sys.modules["astrbot.api.event"] = fake_event_mod
    sys.modules["astrbot.api.message_components"] = fake_components
    sys.modules["astrbot.api.star"] = fake_star_mod
    sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
    sys.modules["astrbot.core.platform"] = types.ModuleType("astrbot.core.platform")
    sys.modules["astrbot.core.platform.message_session"] = fake_platform_mod
    sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")
    sys.modules["astrbot.core.utils.io"] = fake_io
    sys.modules["astrbot.core.utils.session_waiter"] = fake_waiter


_install_astrbot_stubs()
sys.path.insert(0, str(PLUGINS_DIR))


import pytest  # noqa: E402

from astrbot_plugin_course.storage import CourseStorage  # noqa: E402
from astrbot_plugin_course.course_types import UserBinding  # noqa: E402


class FakeCronManager:
    async def list_jobs(self, tag):
        return []

    async def delete_job(self, job_id):
        pass


class FakeContext:
    def __init__(self):
        self.cron_manager = FakeCronManager()
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append(chain)


class FakeEvent:
    def __init__(self, uid, unified_msg_origin=None):
        self._uid = uid
        self.message_str = ""
        self.unified_msg_origin = unified_msg_origin or f"test:{uid}"

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return "tester"

    def plain_result(self, msg):
        return msg

    def image_result(self, url):
        return ("image", url)

    def stop_event(self):
        pass


def make_binding(uid: str, minutes: int = 15) -> UserBinding:
    return UserBinding(
        user_id=uid,
        unified_msg_origin=f"test:{uid}",
        nickname=uid,
        ics_file=f"ics/{uid}.ics",
        updated_at_ts=0.0,
        reminder_advance_minutes=minutes,
    )


@pytest.fixture
def storage(tmp_path):
    """CourseStorage 实例,数据目录重定向到 pytest 临时目录。"""
    _BASE[0] = str(tmp_path)
    return CourseStorage("astrbot_plugin_course")


@pytest.fixture
def plugin(tmp_path):
    """CoursePlugin 实例(含 FakeContext),数据目录重定向到 pytest 临时目录。"""
    from astrbot_plugin_course.main import CoursePlugin

    _BASE[0] = str(tmp_path)
    return CoursePlugin(FakeContext())
