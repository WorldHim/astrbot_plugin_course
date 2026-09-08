"""帮助内容:指令分组数据、文字帮助文本与图片渲染数据。

独立成模块,使 /课表帮助 的文案与 main.py 的逻辑解耦;
文字兜底(help_text)与图片渲染(help_render_data)共用 HELP_SECTIONS 单一数据源。
"""
from __future__ import annotations

from typing import Any

# 指令按功能分组:(组名, [(指令, 说明), ...])
HELP_SECTIONS = [
    (
        "课表管理",
        [
            ("/绑定课表", "绑定个人课表（在 120 秒内发送 .ics 文件）"),
            ("/删除课表", "删除已绑定的课表"),
        ],
    ),
    (
        "课表查询",
        [
            ("/今日课表", "查看今日课程"),
            ("/明日课表", "查看明日课程"),
            ("/本周课表", "查看本周课程"),
            ("/下周课表", "查看下周课程"),
            ("/当前课程", "视奸现在在上什么课"),
            ("/上课时长榜", "查看本群今日卷王"),
            ("/上课时长周榜", "查看本群本周卷王"),
        ],
    ),
    (
        "配置管理",
        [
            ("/设置每日推送", "每日定时推送课表（回复“开启 HH:MM”或“关闭”）"),
            ("/设置提醒时间", "开课前提醒的提前分钟数（1～120）"),
            ("/设置时区", "设置课表时区（IANA 名称，默认东八区）"),
            ("/查看设置", "查看当前配置"),
        ],
    ),
]

HELP_TIPS = [
    "💡 课表 .ics 文件用「课表转日历」工具导出。",
    "📖 发送 /课表帮助 查看本列表。",
]

# 工具链接单独一条消息发送:图片中的长链接无法点击,文本消息中的链接才可打开
TOOL_LINK = (
    "🔗 课表转日历（导出 .ics 课表文件）：\n"
    "https://wikilake.netlify.app/tools/%E8%AF%BE%E8%A1%A8%E8%BD%AC%E6%97%A5%E5%8E%86"
)


def help_text() -> str:
    """插件指令帮助文本(供 /课表帮助 渲染失败时兜底)。"""
    lines = ["📚 早安课表 · 指令列表", ""]
    for name, commands in HELP_SECTIONS:
        lines.append(f"【{name}】")
        lines.extend(f"{cmd}：{desc}" for cmd, desc in commands)
        lines.append("")
    lines.extend(HELP_TIPS)
    return "\n".join(lines)


def help_render_data(version: str) -> dict:
    """构建 /课表帮助 的图片渲染数据。

    数据带插件版本号:同版本内容不变 → 渲染缓存键相同 → 直接复用图片。
    version 由调用方(main.PLUGIN_VERSION)传入,避免本模块反向依赖 main。
    """
    return {
        "title": "早安课表 · 指令列表",
        "version": version,
        "sections": [
            {
                "name": name,
                "commands": [{"cmd": cmd, "desc": desc} for cmd, desc in commands],
            }
            for name, commands in HELP_SECTIONS
        ],
        "tips": list(HELP_TIPS),
        "page_width": 420,
    }