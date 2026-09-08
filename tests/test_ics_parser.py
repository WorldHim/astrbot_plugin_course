"""IcsParser 单元测试:解析错误、规则保留、时区、EXDATE、全天事件、缓存。"""
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_course.ics_parser import IcsParser, SHANGHAI_TZ


def write_ics(tmp_path, body, name="test.ics"):
    path = tmp_path / name
    path.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//CN\n" + body + "END:VCALENDAR\n",
        encoding="utf-8",
    )
    return str(path)


def vevent(uid="1", dtstart="20260907T100000Z", dtend="20260907T113500Z",
           summary="课", rrule=None, extra=""):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20260908T000000Z",
             f"DTSTART:{dtstart}", f"DTEND:{dtend}"]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if extra:
        lines.append(extra)
    lines += [f"SUMMARY:{summary}", "END:VEVENT"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def parser():
    return IcsParser()


class TestParseErrors:
    @pytest.fixture(autouse=True)
    def _inject(self, parser):
        self.parser = parser

    def test_missing_file_returns_none(self, tmp_path):
        assert self.parser.parse_ics_file(str(tmp_path / "no.ics")) is None

    def test_invalid_format_returns_none(self, tmp_path):
        p = tmp_path / "bad.ics"
        p.write_text("this is not a calendar", encoding="utf-8")
        assert self.parser.parse_ics_file(str(p)) is None

    def test_utf8_bom_parsed(self, tmp_path):
        # 部分 .ics 导出工具(及 Windows 记事本)会写 UTF-8 BOM,须能正常解析
        p = tmp_path / "bom.ics"
        content = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//CN\n" + vevent(summary="高数") + "END:VCALENDAR\n"
        p.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        series = self.parser.parse_ics_file(str(p))
        assert series is not None and len(series) == 1
        assert series[0].summary == "高数"

    def test_no_bom_still_parsed(self, tmp_path):
        # 无 BOM 的常规文件不受影响
        series = self.parser.parse_ics_file(write_ics(tmp_path, vevent(summary="英语")))
        assert series is not None and series[0].summary == "英语"

    def test_gbk_ics_parsed(self, tmp_path):
        # Windows 记事本 ANSI(GBK/GB18030)保存的课表 → 统一转 UTF-8 后解析
        content = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//CN\n" + vevent(summary="高等数学") + "END:VCALENDAR\n"
        p = tmp_path / "gbk.ics"
        p.write_bytes(content.encode("gb18030"))
        series = self.parser.parse_ics_file(str(p))
        assert series is not None and len(series) == 1
        assert series[0].summary == "高等数学"

    def test_utf16_ics_parsed(self, tmp_path):
        content = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//CN\n" + vevent(summary="大学英语") + "END:VCALENDAR\n"
        p = tmp_path / "u16.ics"
        p.write_bytes(content.encode("utf-16"))  # 自带 BOM
        series = self.parser.parse_ics_file(str(p))
        assert series is not None and len(series) == 1
        assert series[0].summary == "大学英语"

    def test_unsupported_encoding_returns_none(self, tmp_path):
        p = tmp_path / "garbage.ics"
        p.write_bytes(b"\xff\xff\xff\xff")  # UTF-8/GB18030 均无法解码
        assert self.parser.parse_ics_file(str(p)) is None

    def test_decode_bytes_to_text_units(self):
        from astrbot_plugin_course.course_types import decode_bytes_to_text

        assert decode_bytes_to_text("你好".encode("utf-8")) == "你好"
        assert decode_bytes_to_text(b"\xef\xbb\xbf" + "你好".encode("utf-8")) == "你好"
        assert decode_bytes_to_text("你好".encode("utf-16")) == "你好"
        assert decode_bytes_to_text("你好".encode("gb18030")) == "你好"
        assert decode_bytes_to_text(b"\xff\xff\xff") is None  # 所有候选均失败

    def test_empty_calendar_returns_empty_list(self, tmp_path):
        p = write_ics(tmp_path, "")
        result = self.parser.parse_ics_file(p)
        assert result == [] and result is not None


class TestRules:
    @pytest.fixture(autouse=True)
    def _inject(self, parser):
        self.parser = parser

    def test_rrule_kept_not_preexpanded(self, tmp_path):
        p = write_ics(tmp_path, vevent(rrule="FREQ=WEEKLY;COUNT=3"))
        series = self.parser.parse_ics_file(p)
        assert len(series) == 1
        assert series[0].rrule_text == "FREQ=WEEKLY;COUNT=3"

    def test_single_event_no_rrule(self, tmp_path):
        p = write_ics(tmp_path, vevent())
        series = self.parser.parse_ics_file(p)
        assert len(series) == 1 and series[0].rrule_text is None

    def test_tzid_preserved(self, tmp_path):
        body = ("BEGIN:VEVENT\nUID:1\nDTSTAMP:20260908T000000Z\n"
                "DTSTART;TZID=America/New_York:20260907T100000\n"
                "DTEND;TZID=America/New_York:20260907T113500\n"
                "SUMMARY:课\nEND:VEVENT\n")
        p = write_ics(tmp_path, body)
        series = self.parser.parse_ics_file(p)
        assert "America/New_York" in str(series[0].dtstart.tzinfo)

    def test_naive_uses_default_tz(self, tmp_path):
        p = write_ics(tmp_path, vevent(dtstart="20260907T080000", dtend="20260907T093500"))
        series = self.parser.parse_ics_file(p, default_tz=ZoneInfo("America/New_York"))
        assert series[0].dtstart.utcoffset() == timedelta(hours=-4)

    def test_until_date_handled(self, tmp_path):
        p = write_ics(tmp_path, vevent(rrule="FREQ=WEEKLY;UNTIL=20260921T000000Z"))
        series = self.parser.parse_ics_file(p)
        assert series[0].rrule_text and "UNTIL" in series[0].rrule_text


class TestExdate:
    @pytest.fixture(autouse=True)
    def _inject(self, parser):
        self.parser = parser

    def test_exdate_collected_as_utc(self, tmp_path):
        p = write_ics(tmp_path, vevent(
            "1", "20260907T100000Z", "20260907T113500Z",
            rrule="FREQ=WEEKLY;COUNT=4", extra="EXDATE:20260914T100000Z",
        ))
        series = self.parser.parse_ics_file(p)
        assert len(series[0].exdates_utc) == 1

    def test_exdate_multi_value(self, tmp_path):
        p = write_ics(tmp_path, vevent(
            "1", "20260907T100000Z", "20260907T113500Z",
            rrule="FREQ=WEEKLY;COUNT=4",
            extra="EXDATE:20260914T100000Z,20260921T100000Z",
        ))
        series = self.parser.parse_ics_file(p)
        assert len(series[0].exdates_utc) == 2


class TestAllDay:
    @pytest.fixture(autouse=True)
    def _inject(self, parser):
        self.parser = parser

    def test_single_day_exclusive_dtend(self, tmp_path):
        body = ("BEGIN:VEVENT\nUID:1\nDTSTAMP:20260908T000000Z\n"
                "DTSTART;VALUE=DATE:20260907\nDTEND;VALUE=DATE:20260908\n"
                "SUMMARY:全天\nEND:VEVENT\n")
        series = self.parser.parse_ics_file(write_ics(tmp_path, body))
        assert series[0].all_day is True
        assert series[0].duration == timedelta(days=1)

    def test_multi_day(self, tmp_path):
        body = ("BEGIN:VEVENT\nUID:1\nDTSTAMP:20260908T000000Z\n"
                "DTSTART;VALUE=DATE:20260907\nDTEND;VALUE=DATE:20260910\n"
                "SUMMARY:三天\nEND:VEVENT\n")
        series = self.parser.parse_ics_file(write_ics(tmp_path, body))
        assert series[0].duration == timedelta(days=3)

    def test_no_dtend_defaults_one_day(self, tmp_path):
        body = ("BEGIN:VEVENT\nUID:1\nDTSTAMP:20260908T000000Z\n"
                "DTSTART;VALUE=DATE:20260907\nSUMMARY:无结束\nEND:VEVENT\n")
        series = self.parser.parse_ics_file(write_ics(tmp_path, body))
        assert series[0].all_day is True
        assert series[0].duration == timedelta(days=1)


class TestCache:
    @pytest.fixture(autouse=True)
    def _inject(self, parser):
        self.parser = parser

    def test_same_mtime_hits_cache(self, tmp_path):
        p = write_ics(tmp_path, vevent())
        first = self.parser.parse_ics_file(p)
        second = self.parser.parse_ics_file(p)
        assert first is second

    def test_mtime_change_reparse(self, tmp_path):
        import os
        import time

        p = write_ics(tmp_path, vevent(summary="旧"))
        self.parser.parse_ics_file(p)
        time.sleep(0.01)
        Path(p).write_text(
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//CN\n"
            + vevent(summary="新") + "END:VCALENDAR\n",
            encoding="utf-8",
        )
        os.utime(p, (time.time() + 5, time.time() + 5))
        series = self.parser.parse_ics_file(p)
        assert series[0].summary == "新"

    def test_multi_tz_cache_entries_independent(self, tmp_path):
        p = write_ics(tmp_path, vevent(dtstart="20260907T080000", dtend="20260907T093500"))
        a = self.parser.parse_ics_file(p, default_tz=SHANGHAI_TZ)
        b = self.parser.parse_ics_file(p, default_tz=ZoneInfo("America/New_York"))
        assert a[0].dtstart.utcoffset() == timedelta(hours=8)
        assert b[0].dtstart.utcoffset() == timedelta(hours=-4)

    def test_clear_cache(self, tmp_path):
        p = write_ics(tmp_path, vevent())
        self.parser.parse_ics_file(p)
        self.parser.clear_cache(p)
        assert all(k[0] != p for k in self.parser._cache)

