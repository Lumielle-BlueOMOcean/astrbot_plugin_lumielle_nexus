import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import TaskManager, Storage
import core as core_module


class Phase5StorageAndCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(self.storage, timezone_name="Asia/Shanghai")
        await self.manager.bind_group("班群", "123456789", "qq-main", "operator")

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def test_migration_creates_workflow_and_identity_tables(self):
        tables = {
            row[0] for row in self.storage._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            ).fetchall()
        }
        self.assertIn("workflow_messages", tables)
        self.assertIn("member_identity_fields", tables)

    async def test_natural_message_is_captured_without_deterministic_write(self):
        start = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
        task = await self.manager.start_collection(
            "班群", "返校", ["姓名", "返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=start,
        )
        capture = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我7号下午三点回来",
            sent_at=start + timedelta(minutes=1), source_message_id="m1",
        )
        self.assertFalse(capture["deterministic_handled"])
        self.assertEqual(self.storage.list_entries(task["id"]), [])
        self.assertEqual(self.storage.list_workflow_messages(task["id"])[0]["id"], capture["id"])

    async def test_legacy_submission_prefix_is_not_an_immediate_llm_trigger(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
        )
        capture = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "提交：我7号下午回来",
            source_message_id="m-prefix",
        )
        self.assertFalse(capture["deterministic_handled"])
        self.assertEqual(self.storage.list_entries(task["id"]), [])
        self.assertEqual(len(self.storage.list_workflow_messages(task["id"])), 1)

    async def test_structured_message_is_saved_and_learns_identity(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["姓名", "学号", "返校时间"], "", False,
            "qq-main", "operator", "origin",
        )
        capture = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "姓名：张三\n学号：2026123456\n返校时间：7号",
            source_message_id="m2",
        )
        self.assertTrue(capture["deterministic_handled"])
        entry = self.storage.get_entry(task["id"], "1001")
        self.assertEqual(json.loads(entry["parsed_data"])["姓名"], "张三")
        identity = self.storage.list_identity_fields("qq-main", "123456789", "1001")
        self.assertEqual({row["field_name"]: row["value"] for row in identity}, {
            "姓名": "张三", "学号": "2026123456",
        })

    async def test_checkpoint_snapshot_uses_cursor_and_excludes_handled_messages(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["姓名", "返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
        )
        first = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "姓名：张三", source_message_id="m3",
        )
        second = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我7号下午回来", source_message_id="m4",
        )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        self.assertEqual(snapshot["cursor_id"], 0)
        self.assertEqual(snapshot["next_cursor_id"], second["id"])
        self.assertEqual([row["id"] for row in snapshot["messages"]], [second["id"]])
        self.assertNotEqual(first["id"], second["id"])

    async def test_checkpoint_candidate_rejects_cross_user_evidence(self):
        accepted = core_module.validate_collection_checkpoint_candidate(
            {
                "members": [{
                    "user_id": "1001", "status": "ok", "items": [{
                        "field": "返校时间", "value": "7号下午",
                        "evidence": "1002说7号下午", "confidence": 0.99,
                    }],
                }],
            },
            ["返校时间"],
            {"1001": ["我明天回来"], "1002": ["1002说7号下午"]},
        )
        self.assertEqual(accepted["members"], [])
        self.assertTrue(accepted["rejected"])

    async def test_successful_checkpoint_advances_cursor_and_updates_entry(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
        )
        message = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我7号下午回来", source_message_id="m5",
        )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        result = await self.manager.apply_collection_checkpoint(
            task["id"], snapshot,
            {"members": [{
                "user_id": "1001", "status": "ok", "items": [{
                    "field": "返校时间", "value": "7号下午",
                    "evidence": "7号下午", "confidence": 0.97,
                }],
            }]},
        )
        self.assertEqual(result["applied"], ["1001"])
        self.assertEqual(json.loads(self.storage.get_entry(task["id"], "1001")["parsed_data"]), {
            "返校时间": "7号下午",
        })
        saved_task = self.storage.get_task(task["id"])
        self.assertEqual(json.loads(saved_task["result"])["analysis_cursor_id"], message["id"])

    async def test_second_checkpoint_without_new_messages_has_empty_snapshot(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
        )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        self.assertEqual(snapshot["messages"], [])
        self.assertEqual(snapshot["next_cursor_id"], 0)

    async def test_missing_default_only_creates_entry_for_members_without_any_entry(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["返校状态"], "", False,
            "qq-main", "operator", "origin",
        )
        await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "返校状态：已返校",
        )
        result = await self.manager.apply_missing_default(
            task["id"], {"1001", "1002"}, "返校状态", "未返校",
        )
        self.assertEqual(result["defaulted_ids"], ["1002"])
        self.assertEqual(json.loads(self.storage.get_entry(task["id"], "1001")["parsed_data"]), {
            "返校状态": "已返校",
        })
        self.assertEqual(json.loads(self.storage.get_entry(task["id"], "1002")["parsed_data"]), {
            "返校状态": "未返校",
        })

    async def test_verified_identity_is_not_overwritten_by_unverified_learning(self):
        await self.manager.set_member_identity(
            "班群", "1001", "张三", "", "qq-main", "operator",
        )
        await self.manager.learn_identity(
            "qq-main", "123456789", "1001", {"姓名": "李四"}, "checkpoint_llm",
        )
        fields = self.storage.list_identity_fields("qq-main", "123456789", "1001")
        self.assertEqual(fields[0]["value"], "张三")
        self.assertEqual(fields[0]["verified"], 1)


class Phase5PureContractTests(unittest.TestCase):
    def test_group_admin_role_is_strict(self):
        self.assertTrue(core_module.is_group_admin_member({"role": "owner"}))
        self.assertTrue(core_module.is_group_admin_member({"role": "admin"}))
        self.assertFalse(core_module.is_group_admin_member({"role": "member"}))
        self.assertFalse(core_module.is_group_admin_member({}))

    def test_conversation_hints_find_task_id_and_alias_without_llm(self):
        hints = core_module.extract_collection_reference_hints(
            "刚才的班群返校统计 C-20260909-001 怎么样？",
            ["班群", "班委群"],
        )
        self.assertEqual(hints["task_ids"], ["C-20260909-001"])
        self.assertEqual(hints["aliases"], ["班群"])
