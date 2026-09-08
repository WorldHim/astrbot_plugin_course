from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Set

from astrbot.api import logger
from astrbot.api.star import StarTools

from .course_types import UserBinding


class CourseStorage:
    def __init__(self, plugin_name: str):
        self._plugin_name = plugin_name
        self._base_dir = StarTools.get_data_dir(plugin_name)
        self._ics_dir = self._base_dir / "ics"
        self._bindings_file = self._base_dir / "bindings.json"
        self._reminded_file = self._base_dir / "reminded.json"

        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._ics_dir.mkdir(parents=True, exist_ok=True)

    def get_ics_path(self, user_id: str) -> Path:
        safe_name = "".join(ch for ch in user_id if ch.isalnum() or ch in ("-", "_"))
        if not safe_name:
            safe_name = "user"
        return self._ics_dir / f"{safe_name}.ics"

    def resolve_ics_path(self, binding: UserBinding) -> Path:
        """由绑定记录解析其课表文件的绝对路径。"""
        return (self._base_dir / binding.ics_file).resolve()

    def _read_bindings_json(self, path: Path) -> Dict[str, UserBinding]:
        """解析绑定文件;缺失/损坏时抛异常(由调用方决定是否回退备份)。"""
        raw = json.loads(path.read_text(encoding="utf-8"))
        bindings: Dict[str, UserBinding] = {}
        for user_id, item in raw.get("bindings", {}).items():
            if not isinstance(item, dict):
                continue
            try:
                bindings[user_id] = UserBinding(
                    user_id=user_id,
                    unified_msg_origin=str(item.get("unified_msg_origin", "")),
                    nickname=str(item.get("nickname", "")),
                    ics_file=str(item.get("ics_file", "")),
                    updated_at_ts=float(item.get("updated_at_ts", 0.0)),
                    enable_daily_push=bool(item.get("enable_daily_push", False)),
                    daily_push_time=str(item.get("daily_push_time", "07:00")),
                    reminder_advance_minutes=int(
                        item.get("reminder_advance_minutes", 15)
                    ),
                    daily_push_job_id=str(item.get("daily_push_job_id", "")),
                    timezone_name=str(item.get("timezone_name", "Asia/Shanghai")),
                )
            except Exception as e:
                logger.warning(f"[course] skip invalid binding for {user_id}: {e}")
                continue
        return bindings

    def load_bindings(self) -> Dict[str, UserBinding]:
        """加载绑定;主文件损坏时自动从 .bak 备份恢复并告警。"""
        main_path = self._bindings_file
        bak_path = self._bindings_file.with_suffix(".json.bak")

        if main_path.exists():
            try:
                return self._read_bindings_json(main_path)
            except Exception as e:
                logger.error(
                    f"[course] bindings.json is corrupted, trying backup: {e}"
                )
        else:
            logger.info("[course] bindings.json not found, trying backup...")

        if not bak_path.exists():
            logger.warning("[course] no bindings backup available")
            return {}

        try:
            restored = self._read_bindings_json(bak_path)
        except Exception as e:
            logger.error(f"[course] bindings backup is also corrupted: {e}")
            return {}

        logger.warning(
            f"[course] restored {len(restored)} bindings from backup "
            f"({bak_path.name})"
        )
        try:
            # 用备份恢复主文件,避免下次仍读到坏文件
            shutil.copyfile(bak_path, main_path)
        except Exception as e:
            logger.warning(f"[course] failed to copy backup to main file: {e}")
        return restored

    def load_reminded(self) -> Dict[str, Set[str]]:
        """加载已提醒记录(开课提醒去重);文件缺失或损坏时返回空记录。"""
        if not self._reminded_file.exists():
            return {}
        try:
            raw = json.loads(self._reminded_file.read_text(encoding="utf-8"))
            reminded: Dict[str, Set[str]] = {}
            for uid, keys in raw.get("reminded", {}).items():
                if not isinstance(keys, list):
                    continue
                reminded[str(uid)] = {str(k) for k in keys}
            return reminded
        except Exception as e:
            logger.error(f"[course] Failed to load reminded.json: {e}")
            return {}

    def save_reminded(self, reminded: Dict[str, Set[str]]) -> None:
        """持久化已提醒记录(临时文件 + replace 原子写,避免写坏)。"""
        payload = {
            "version": 1,
            "updated_at_ts": time.time(),
            "reminded": {uid: sorted(keys) for uid, keys in reminded.items()},
        }
        tmp_path = self._reminded_file.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp_path.replace(self._reminded_file)
        except Exception as e:
            logger.error(f"[course] Failed to save reminded.json: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def save_bindings(self, bindings: Dict[str, UserBinding]) -> None:
        payload = {
            "version": 1,
            "updated_at_ts": time.time(),
            "bindings": {uid: asdict(b) for uid, b in bindings.items()},
        }
        tmp_path = self._bindings_file.with_suffix(".json.tmp")
        bak_path = self._bindings_file.with_suffix(".json.bak")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # 轮转备份:保留上一版数据,供主文件损坏时恢复
            if self._bindings_file.exists():
                shutil.copyfile(self._bindings_file, bak_path)
            # 临时文件 + replace 原子写,避免写一半崩溃导致文件损坏
            tmp_path.replace(self._bindings_file)
        except Exception as e:
            logger.error(f"[course] Failed to save bindings.json: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def delete_binding(self, user_id: str) -> bool:
        bindings = self.load_bindings()
        binding = bindings.pop(user_id, None)
        if not binding:
            return False
        self.save_bindings(bindings)
        try:
            ics_path = self._base_dir / binding.ics_file
            if ics_path.exists():
                ics_path.unlink()
        except Exception as e:
            logger.warning(f"[course] Failed to delete ics file: {e}")
        return True

    def upsert_binding(
        self,
        *,
        user_id: str,
        unified_msg_origin: str,
        nickname: str,
        default_reminder_minutes: int = 15,
        default_push_time: str = "07:00",
        default_timezone: str = "Asia/Shanghai",
    ) -> UserBinding:
        bindings = self.load_bindings()
        prev = bindings.get(user_id)
        ics_path = self.get_ics_path(user_id)
        rel_ics = str(ics_path.relative_to(self._base_dir)).replace("\\", "/")
        binding = UserBinding(
            user_id=user_id,
            unified_msg_origin=unified_msg_origin,
            nickname=nickname,
            ics_file=rel_ics,
            updated_at_ts=time.time(),
            enable_daily_push=prev.enable_daily_push if prev else False,
            daily_push_time=prev.daily_push_time if prev else default_push_time,
            reminder_advance_minutes=(
                prev.reminder_advance_minutes if prev else default_reminder_minutes
            ),
            daily_push_job_id=prev.daily_push_job_id if prev else "",
            timezone_name=prev.timezone_name if prev else default_timezone,
        )
        bindings[user_id] = binding
        self.save_bindings(bindings)
        return binding

    def get_binding(self, user_id: str) -> Optional[UserBinding]:
        return self.load_bindings().get(user_id)
