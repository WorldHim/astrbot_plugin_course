from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Set

from astrbot.api import logger
from astrbot.api.star import StarTools

from .course_types import UserBinding, decode_bytes_to_text


class CourseStorage:
    def __init__(self, plugin_name: str):
        self._plugin_name = plugin_name
        self._base_dir = StarTools.get_data_dir(plugin_name)
        self._ics_dir = self._base_dir / "ics"
        self._bindings_file = self._base_dir / "bindings.json"
        self._reminded_file = self._base_dir / "reminded.json"
        # (主文件 mtime, 绑定数据)——文件未变化时 load 直接返回缓存,省去磁盘 IO
        self._bindings_cache: Optional[tuple[float, Dict[str, UserBinding]]] = None

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
        """解析绑定文件;缺失/损坏/编码不支持时抛异常(由调用方决定是否回退备份)。

        编码自动识别(UTF-8 ±BOM / UTF-16 / GBK→统一转为 UTF-8 文本),
        兼容手工编辑(如 Windows 记事本 ANSI/UTF-16 保存)的文件。
        """
        raw_text = decode_bytes_to_text(path.read_bytes())
        if raw_text is None:
            raise ValueError("unsupported file encoding")
        raw = json.loads(raw_text)
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
                    # 旧版数据无该字段 → 默认 False(开课提醒默认关闭)
                    enable_reminder=bool(item.get("enable_reminder", False)),
                    reminder_advance_minutes=int(
                        item.get("reminder_advance_minutes", 15)
                    ),
                    daily_push_job_id=str(item.get("daily_push_job_id", "")),
                    timezone_name=str(item.get("timezone_name", "Asia/Shanghai")),
                    # 旧版数据无该字段 → 空串(渲染回退 qlogo 推导/隐藏占位)
                    avatar=str(item.get("avatar", "")),
                )
            except Exception as e:
                logger.warning(f"[course] skip invalid binding for {user_id}: {e}")
                continue
        return bindings

    def load_bindings(self) -> Dict[str, UserBinding]:
        """加载绑定;带 mtime 内存缓存(文件未变化时直接返回缓存对象,勿直接修改)。"""
        try:
            mtime: Optional[float] = self._bindings_file.stat().st_mtime
        except OSError:
            mtime = None

        cached = self._bindings_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]

        mtime, bindings = self._read_bindings_with_recovery()
        self._bindings_cache = (mtime, bindings)
        return bindings

    def _read_bindings_with_recovery(
        self,
    ) -> tuple[Optional[float], Dict[str, UserBinding]]:
        """从磁盘读取绑定;主文件损坏时自动从 .bak 备份恢复并告警。"""
        main_path = self._bindings_file
        bak_path = self._bindings_file.with_suffix(".json.bak")

        try:
            mtime = main_path.stat().st_mtime
        except OSError:
            mtime = None

        if main_path.exists():
            try:
                return mtime, self._read_bindings_json(main_path)
            except Exception as e:
                logger.error(
                    f"[course] bindings.json is corrupted, trying backup: {e}"
                )
        else:
            logger.info("[course] bindings.json not found, trying backup...")

        if not bak_path.exists():
            logger.warning("[course] no bindings backup available")
            return mtime, {}

        try:
            restored = self._read_bindings_json(bak_path)
        except Exception as e:
            logger.error(f"[course] bindings backup is also corrupted: {e}")
            return mtime, {}

        logger.warning(
            f"[course] restored {len(restored)} bindings from backup "
            f"({bak_path.name})"
        )
        try:
            # 用备份恢复主文件,避免下次仍读到坏文件
            shutil.copyfile(bak_path, main_path)
            mtime = main_path.stat().st_mtime
        except Exception as e:
            logger.warning(f"[course] failed to copy backup to main file: {e}")
        return mtime, restored

    def load_reminded(self) -> Dict[str, Set[str]]:
        """加载已提醒记录(开课提醒去重);文件缺失或损坏时返回空记录。"""
        if not self._reminded_file.exists():
            return {}
        try:
            # 编码自动识别(含 GBK/UTF-16),统一转为 UTF-8 文本
            raw_text = decode_bytes_to_text(self._reminded_file.read_bytes())
            if raw_text is None:
                raise ValueError("unsupported file encoding")
            raw = json.loads(raw_text)
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
            # 保存成功,同步刷新内存缓存(省一次回读)
            try:
                self._bindings_cache = (
                    self._bindings_file.stat().st_mtime,
                    dict(bindings),
                )
            except OSError:
                self._bindings_cache = None
        except Exception as e:
            logger.error(f"[course] Failed to save bindings.json: {e}")
            # 保存失败时缓存状态不确定,置空强制下次重读
            self._bindings_cache = None
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
        default_reminder_enabled: bool = False,
        default_reminder_minutes: int = 15,
        default_push_time: str = "07:00",
        default_timezone: str = "Asia/Shanghai",
        avatar: str = "",
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
            enable_reminder=(
                prev.enable_reminder if prev else default_reminder_enabled
            ),
            reminder_advance_minutes=(
                prev.reminder_advance_minutes if prev else default_reminder_minutes
            ),
            daily_push_job_id=prev.daily_push_job_id if prev else "",
            timezone_name=prev.timezone_name if prev else default_timezone,
            # 本次没提取到头像时保留旧值(qq_official payload 不一定下发 avatar)
            avatar=avatar or (prev.avatar if prev else ""),
        )
        bindings[user_id] = binding
        self.save_bindings(bindings)
        return binding

    def get_binding(self, user_id: str) -> Optional[UserBinding]:
        return self.load_bindings().get(user_id)
