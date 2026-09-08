"""CourseStorage 单元测试:原子写、备份恢复、绑定迁移、reminded 往返。"""
import json

from conftest import make_binding


class TestBindings:
    def test_load_missing_returns_empty(self, storage):
        assert storage.load_bindings() == {}

    def test_save_load_roundtrip(self, storage):
        storage.save_bindings({"u1": make_binding("u1", 30)})
        loaded = storage.load_bindings()
        assert loaded["u1"].reminder_advance_minutes == 30
        assert loaded["u1"].timezone_name == "Asia/Shanghai"

    def test_second_save_creates_bak_with_previous_version(self, storage):
        storage.save_bindings({"u1": make_binding("u1", 15)})
        storage.save_bindings({"u1": make_binding("u1", 30), "u2": make_binding("u2")})
        bak = storage._bindings_file.with_suffix(".json.bak")
        assert bak.exists()
        data = json.loads(bak.read_text(encoding="utf-8"))
        assert set(data["bindings"]) == {"u1"}
        assert data["bindings"]["u1"]["reminder_advance_minutes"] == 15

    def test_corrupted_main_restored_from_bak(self, storage):
        storage.save_bindings({"u1": make_binding("u1")})
        storage.save_bindings({"u1": make_binding("u1", 30), "u2": make_binding("u2")})
        storage._bindings_file.write_text("corrupted", encoding="utf-8")

        restored = storage.load_bindings()

        # 恢复到上一版(仅 u1)
        assert set(restored) == {"u1"}
        assert restored["u1"].reminder_advance_minutes == 15
        # 主文件已被备份内容还原
        assert "corrupted" not in storage._bindings_file.read_text(encoding="utf-8")
        assert storage.load_bindings().keys() == restored.keys()

    def test_missing_main_restored_from_bak(self, storage):
        storage.save_bindings({"u1": make_binding("u1")})
        storage.save_bindings({"u1": make_binding("u1", 30)})  # 产生 .bak(上一版)
        storage._bindings_file.unlink()

        restored = storage.load_bindings()

        assert set(restored) == {"u1"}
        assert restored["u1"].reminder_advance_minutes == 15
        assert storage._bindings_file.exists()

    def test_both_corrupted_returns_empty(self, storage):
        storage._bindings_file.write_text("bad", encoding="utf-8")
        storage._bindings_file.with_suffix(".json.bak").write_text("bad2", encoding="utf-8")
        assert storage.load_bindings() == {}

    def test_invalid_item_skipped(self, storage):
        payload = {
            "version": 1,
            "bindings": {
                "good": {
                    "user_id": "good", "unified_msg_origin": "t", "nickname": "",
                    "ics_file": "a", "updated_at_ts": 0.0,
                },
                "bad": {"updated_at_ts": "not-a-number"},
            },
        }
        storage._bindings_file.write_text(json.dumps(payload), encoding="utf-8")
        loaded = storage.load_bindings()
        assert "good" in loaded and "bad" not in loaded

    def test_timezone_name_migration(self, storage):
        payload = {
            "version": 1,
            "bindings": {
                "u": {
                    "user_id": "u", "unified_msg_origin": "t", "nickname": "",
                    "ics_file": "a", "updated_at_ts": 0.0,
                }
            },
        }
        storage._bindings_file.write_text(json.dumps(payload), encoding="utf-8")
        assert storage.load_bindings()["u"].timezone_name == "Asia/Shanghai"

    def test_upsert_new_user_uses_defaults(self, storage):
        b = storage.upsert_binding(
            user_id="u", unified_msg_origin="t", nickname="n",
            default_reminder_minutes=30, default_push_time="08:30",
            default_timezone="America/New_York",
        )
        assert b.reminder_advance_minutes == 30
        assert b.daily_push_time == "08:30"
        assert b.timezone_name == "America/New_York"

    def test_upsert_existing_keeps_custom(self, storage):
        b = storage.upsert_binding(user_id="u", unified_msg_origin="t", nickname="n")
        b.reminder_advance_minutes = 45
        b.timezone_name = "Europe/London"
        storage.save_bindings({"u": b})
        b2 = storage.upsert_binding(
            user_id="u", unified_msg_origin="t", nickname="n",
            default_reminder_minutes=30, default_push_time="08:30",
            default_timezone="America/New_York",
        )
        assert b2.reminder_advance_minutes == 45
        assert b2.timezone_name == "Europe/London"

    def test_delete_binding_removes_file(self, storage):
        b = storage.upsert_binding(user_id="u", unified_msg_origin="t", nickname="n")
        ics = storage._base_dir / "ics" / "u.ics"
        ics.parent.mkdir(parents=True, exist_ok=True)
        ics.write_text("x", encoding="utf-8")

        assert storage.delete_binding("u") is True
        assert not ics.exists()
        assert storage.get_binding("u") is None
        assert storage.delete_binding("u") is False

    def test_resolve_ics_path(self, storage):
        b = storage.upsert_binding(user_id="u", unified_msg_origin="t", nickname="n")
        assert storage.resolve_ics_path(b) == (storage._base_dir / "ics" / "u.ics").resolve()


class TestReminded:
    def test_roundtrip(self, storage):
        storage.save_reminded({"u": {"k1", "k2"}})
        assert storage.load_reminded() == {"u": {"k1", "k2"}}

    def test_missing_returns_empty(self, storage):
        assert storage.load_reminded() == {}

    def test_corrupted_returns_empty(self, storage):
        storage._reminded_file.write_text("bad", encoding="utf-8")
        assert storage.load_reminded() == {}

    def test_atomic_no_tmp_left(self, storage):
        storage.save_reminded({"u": {"k"}})
        assert not storage._reminded_file.with_suffix(".json.tmp").exists()
