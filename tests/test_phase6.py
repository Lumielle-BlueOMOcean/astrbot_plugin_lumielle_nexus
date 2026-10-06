import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook

import core as core_module
from core import TaskManager, Storage, collection_member_stats
from exporter import export_collection


class Phase6CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(self.storage, timezone_name="Asia/Shanghai")
        await self.manager.bind_group("班群", "123456789", "qq-main", "operator")
        self.now = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def test_collection_children_persist_checkpoint_and_finalize_schedule(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["姓名", "状态"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
            chase_at=self.now + timedelta(hours=9),
            deadline=self.now + timedelta(hours=10),
            missing_default_field="状态",
            missing_default_value="未提交",
            auto_export=True,
            now=self.now,
        )
        payload = json.loads(task["payload"])
        self.assertEqual(payload["capture_start"], self.now.isoformat(timespec="seconds"))
        self.assertTrue(payload["auto_export"])
        children = self.storage.list_children(task["id"])
        self.assertEqual(len(children), 2)
        self.assertEqual(
            {json.loads(child["payload"])["kind"] for child in children},
            {"collection_checkpoint", "collection_finalize"},
        )

    async def test_capture_respects_deadline_and_source_id_idempotency(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["状态"], "", False,
            "qq-main", "operator", "origin", now=self.now,
            deadline=self.now + timedelta(hours=1),
        )
        self.assertIsNone(await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "状态：已返校",
            sent_at=self.now + timedelta(hours=1, seconds=1), source_message_id="late",
        ))
        first = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "状态：已返校", source_message_id="same",
            now=self.now + timedelta(minutes=1),
        )
        duplicate = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "状态：已返校", source_message_id="same",
            now=self.now + timedelta(minutes=1),
        )
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(len(self.storage.list_workflow_messages(task["id"])), 1)

    async def test_collection_target_snapshot_does_not_follow_later_set_updates(self):
        members = [
            {"user_id": "1001", "nickname": "张三"},
            {"user_id": "1002", "nickname": "李四"},
        ]
        await self.manager.set_member_set(
            "班群", "班委", ["1001"], "replace", "qq-main", "operator", members,
        )
        task = await self.manager.start_collection(
            "班群", "返校", ["状态"], "", False,
            "qq-main", "operator", "origin", target_member_set="班委", now=self.now,
        )
        await self.manager.set_member_set(
            "班群", "班委", ["1002"], "replace", "qq-main", "operator", members,
        )
        payload = json.loads(self.storage.get_task(task["id"])["payload"])
        self.assertEqual(payload["target_member_ids"], ["1001"])

    async def test_collection_allowed_value_mapping_is_deterministic(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["是否返校"], "", False,
            "qq-main", "operator", "origin", now=self.now,
        )
        result = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "已返校", source_message_id="return-1",
            now=self.now + timedelta(minutes=1),
        )
        self.assertTrue(result["deterministic_handled"])
        saved = self.storage.get_entry(task["id"], "1001")
        self.assertEqual(json.loads(saved["parsed_data"]), {"是否返校": "已返校"})

    async def test_pending_checkpoint_rows_are_oldest_first_and_resume_after_batch(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["状态"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=self.now,
        )
        for index in range(3):
            await self.manager.capture_collection_message(
                task["id"], "1001", "张三", f"消息 {index}",
                source_message_id=f"resume-{index}", now=self.now + timedelta(minutes=1),
            )
        with mock.patch.object(core_module, "COLLECTION_CHECKPOINT_MAX_MESSAGES", 2):
            first = await self.manager.prepare_collection_checkpoint(
                task["id"], self.now + timedelta(minutes=1), "manual",
            )
            self.assertEqual([row["id"] for row in first["messages"]], [1, 2])
            await self.manager.apply_collection_checkpoint(task["id"], first, {
                "members": [
                    {"user_id": "1001", "status": "no_data", "items": []},
                ],
            })
            second = await self.manager.prepare_collection_checkpoint(
                task["id"], self.now + timedelta(minutes=1), "manual",
            )
        self.assertEqual([row["id"] for row in second["messages"]], [3])
        self.assertEqual(second["pending_message_count"], 1)

    async def test_unresolved_rows_survive_restart_as_pending_evidence(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["状态"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=self.now,
        )
        captured = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我晚点回", source_message_id="pending-1",
            now=self.now + timedelta(minutes=1),
        )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], self.now + timedelta(minutes=1), "manual",
        )
        await self.manager.apply_collection_checkpoint(task["id"], snapshot, {"members": []})
        row = self.storage.list_workflow_messages(task["id"])[0]
        self.assertEqual(row["analysis_state"], "error")
        self.assertEqual(self.storage.count_pending_workflow_messages(task["id"]), 1)
        self.assertEqual(self.storage.workflow_analysis_cursor(task["id"]), 0)
        with self.assertRaisesRegex(ValueError, "未解决"):
            await self.manager.complete_collection(task["id"], {})

    async def test_checkpoint_message_cap_keeps_oldest_rows_pending_after_batch(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["状态"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=self.now,
        )
        for index in range(501):
            await self.manager.capture_collection_message(
                task["id"], "1001", "张三", f"自然语言 {index}",
                source_message_id=f"m-{index}", now=self.now + timedelta(minutes=1),
            )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], self.now + timedelta(minutes=1), "manual",
        )
        self.assertEqual(len(snapshot["messages"]), core_module.COLLECTION_CHECKPOINT_MAX_MESSAGES)
        self.assertEqual(snapshot["messages"][0]["id"], 1)
        self.assertEqual(snapshot["messages"][-1]["id"], 500)
        self.assertEqual(snapshot["next_cursor_id"], 500)
        self.assertEqual(snapshot["pending_message_count"], 501)

    async def test_character_budget_keeps_omitted_messages_pending(self):
        task = await self.manager.start_collection(
            "班群", "返校", ["状态"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=self.now,
        )
        for index in range(3):
            await self.manager.capture_collection_message(
                task["id"], "1001", "张三", f"{index}:" + "甲" * 17998,
                source_message_id=f"budget-{index}",
                now=self.now + timedelta(minutes=1),
            )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], self.now + timedelta(minutes=1), "manual",
        )
        self.assertEqual([row["id"] for row in snapshot["messages"]], [1, 2])
        self.assertEqual(snapshot["pending_message_count"], 3)

    async def test_status_tracks_pending_partial_and_complete_members(self):
        stats = collection_member_stats(
            [
                {"user_id": "1001", "nickname": "张三"},
                {"user_id": "1002", "nickname": "李四"},
                {"user_id": "1003", "nickname": "王五"},
                {"user_id": "1004", "nickname": "赵六"},
            ],
            [
                {"sender_id": "1002", "parsed_data": '{"姓名":"李四"}'},
                {"sender_id": "1003", "parsed_data": '{"姓名":"王五"}'},
                {"sender_id": "1004", "parsed_data": '{"姓名":"赵六","学号":"4"}'},
            ],
            self_id="9000",
            required_fields=["姓名", "学号"],
            pending_ids={"1002"},
        )
        self.assertEqual(stats["no_response_ids"], {"1001"})
        self.assertEqual(stats["pending_ids"], {"1002"})
        self.assertEqual(stats["partial_ids"], {"1003"})
        self.assertEqual(stats["complete_ids"], {"1004"})

    async def test_scoped_collection_resolution_does_not_fall_back_to_other_group(self):
        await self.manager.bind_group("社团群", "987654321", "qq-main", "operator")
        await self.manager.start_collection(
            "社团群", "报名", ["姓名"], "", False,
            "qq-main", "operator", "origin", now=self.now,
        )
        with self.assertRaisesRegex(KeyError, "指定群"):
            await self.manager.resolve_collection_reference(
                "qq-main", group="班群",
            )


class Phase6PureAndExportTests(unittest.TestCase):
    def test_target_stats_use_current_human_members_only(self):
        stats = collection_member_stats(
            [
                {"user_id": "bot", "is_robot": True},
                {"user_id": "1001", "nickname": "张三"},
                {"user_id": "1002", "nickname": "李四"},
            ],
            [{"sender_id": "1001"}, {"sender_id": "1009"}],
            self_id="bot",
            target_ids={"1001", "1009"},
        )
        self.assertEqual(stats["eligible_ids"], {"1001"})
        self.assertEqual(stats["submitted_ids"], {"1001"})
        self.assertEqual(stats["missing_ids"], set())

    def test_checkpoint_prompt_is_untrusted_and_taskless(self):
        system = core_module.build_collection_checkpoint_system_prompt()
        prompt = core_module.build_collection_checkpoint_prompt(
            "返校", ["姓名"], "", "2026-09-09T10:00:00+00:00", "Asia/Shanghai",
            [{"user_id": "1001", "messages": [{"message_text": "我明天回"}]}],
        )
        self.assertIn("untrusted data", system)
        self.assertIn("不要调用工具、创建任务", system)
        self.assertIn("<checkpoint_input>", prompt)

    def test_identity_rows_change_export_header_without_duplicating_identity_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = export_collection(
                Path(directory),
                {
                    "id": "C-20260909-001",
                    "group_alias": "班群",
                    "group_id": "123456789",
                    "creator_id": "operator",
                    "created_at": "2026-09-09T10:00:00+00:00",
                    "finished_at": "2026-09-09T11:00:00+00:00",
                    "payload": json.dumps({"title": "返校", "fields": ["姓名", "学号", "状态"]}),
                },
                [{
                    "sender_id": "1001", "sender_name": "群昵称",
                    "parsed_data": json.dumps({"姓名": "张三", "学号": "S1", "状态": "已返校"}),
                    "submitted_at": "2026-09-09T10:10:00+00:00",
                    "updated_at": "2026-09-09T10:10:00+00:00",
                    "raw_message": "状态：已返校",
                }],
                [{"user_id": "1001", "card": "群昵称"}],
                identities=[
                    {"user_id": "1001", "field_name": "姓名", "value": "张三", "verified": 1},
                    {"user_id": "1001", "field_name": "学号", "value": "S1", "verified": 1},
                ],
            )
            workbook = load_workbook(path, read_only=True)
            self.assertEqual(
                [cell.value for cell in next(workbook["统计结果"].iter_rows())],
                ["QQ", "姓名", "学号", "群昵称", "状态", "提交时间", "最后更新时间", "原始提交"],
            )
