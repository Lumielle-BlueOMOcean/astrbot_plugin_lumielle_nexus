import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import core as core_module

from core import TaskManager, validate_moderation_preflight
from qq_adapter import QQAdapter
from storage import Storage


class HardeningPureTests(unittest.IsolatedAsyncioTestCase):
    async def test_mention_batches_persist_each_batch_and_only_first_has_text(self):
        deliver_mention_batches = getattr(core_module, "deliver_mention_batches", None)
        self.assertIsNotNone(deliver_mention_batches)
        if deliver_mention_batches is None:
            return
        sent = []
        persisted = []

        async def send_batch(user_ids, text):
            sent.append((list(user_ids), text))

        async def save_progress(user_ids):
            persisted.append(list(user_ids))

        progress = []
        result = await deliver_mention_batches(
            [str(index) for index in range(45)],
            send_batch,
            save_progress,
            "请尽快提交",
            progress,
        )

        self.assertEqual([len(item[0]) for item in sent], [20, 20, 5])
        self.assertEqual([item[1] for item in sent], ["请尽快提交", "", ""])
        self.assertEqual([len(item) for item in persisted], [20, 40, 45])
        self.assertEqual(result, [str(index) for index in range(45)])
        self.assertEqual(progress, result)

    async def test_mention_batch_failure_leaves_only_successful_progress(self):
        deliver_mention_batches = getattr(core_module, "deliver_mention_batches", None)
        self.assertIsNotNone(deliver_mention_batches)
        if deliver_mention_batches is None:
            return
        sent = []
        persisted = []

        async def send_batch(user_ids, text):
            sent.append((list(user_ids), text))
            if len(sent) == 2:
                raise RuntimeError("OneBot down")

        async def save_progress(user_ids):
            persisted.append(list(user_ids))

        progress = []
        with self.assertRaisesRegex(RuntimeError, "OneBot down"):
            await deliver_mention_batches(
                [str(index) for index in range(45)],
                send_batch,
                save_progress,
                "请尽快提交",
                progress,
            )

        self.assertEqual(len(persisted), 1)
        self.assertEqual(len(progress), 20)
        self.assertEqual(sent[1][1], "")

    async def test_mention_batch_resume_skips_persisted_ids(self):
        deliver_mention_batches = getattr(core_module, "deliver_mention_batches", None)
        self.assertIsNotNone(deliver_mention_batches)
        if deliver_mention_batches is None:
            return
        sent = []

        async def send_batch(user_ids, text):
            sent.append((list(user_ids), text))

        async def save_progress(_user_ids):
            return None

        progress = [str(index) for index in range(1, 21)]
        await deliver_mention_batches(
            [str(index) for index in range(1, 46)],
            send_batch,
            save_progress,
            "请提交",
            progress,
        )
        self.assertEqual(
            [user_id for batch, _text in sent for user_id in batch],
            [str(index) for index in range(21, 46)],
        )
        self.assertTrue(all(text == "" for _batch, text in sent))

    def test_moderation_role_resolution_is_strict(self):
        strict_member_role = getattr(core_module, "strict_member_role", None)
        self.assertIsNotNone(strict_member_role)
        if strict_member_role is None:
            return
        self.assertIsNone(strict_member_role({}))
        self.assertIsNone(strict_member_role({"role": ""}))
        self.assertIsNone(strict_member_role({"role": "unknown"}))
        self.assertIn("无法确认机器人", validate_moderation_preflight(
            {"user_id": "9000"}, {"user_id": "1001", "role": "member"},
            "9000", "1001",
        ) or "")
        self.assertIn("无法确认目标成员", validate_moderation_preflight(
            {"user_id": "9000", "role": "admin"}, {"user_id": "1001"},
            "9000", "1001",
        ) or "")
        self.assertIn("无法确认机器人", validate_moderation_preflight(
            {"user_id": "9000", "role": "unknown"}, {"user_id": "1001", "role": "member"},
            "9000", "1001",
        ) or "")
        self.assertIn("无法确认目标成员", validate_moderation_preflight(
            {"user_id": "9000", "role": "admin"}, {"user_id": "1001", "role": ""},
            "9000", "1001",
        ) or "")


class HardeningStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(self.storage)
        await self.manager.bind_group("班群", "123456789", "qq-main", "A")

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def test_recovery_merges_existing_relay_and_moderation_result(self):
        relay = self.storage.create_task(
            "X-recovery", "RELAY", "PROCESSING", "123456789", "班群", "qq-main",
            "A", "origin", "2026-09-09T00:00:00+00:00", None,
            {"content": "x", "mention_user_ids": ["1", "2"]},
        )
        self.storage.update_task(
            relay["id"], result={"mentioned_user_ids": ["1"]},
            updated_at="2026-09-09T00:01:00+00:00",
        )
        moderation = self.storage.create_task(
            "M-recovery", "MODERATION", "PROCESSING", "123456789", "班群", "qq-main",
            "A", "origin", "2026-09-09T00:00:00+00:00", None,
            {"action": "kick", "target_id": "1"},
        )
        self.storage.update_task(
            moderation["id"], result={"audit_marker": "kept"},
            updated_at="2026-09-09T00:01:00+00:00",
        )

        self.storage.recover_processing_relays("2026-09-09T00:02:00+00:00")
        self.storage.recover_processing_moderations("2026-09-09T00:02:00+00:00")

        relay_result = json.loads(self.storage.get_task(relay["id"])["result"])
        moderation_result = json.loads(self.storage.get_task(moderation["id"])["result"])
        self.assertEqual(relay_result["mentioned_user_ids"], ["1"])
        self.assertEqual(relay_result["delivery_outcome"], "unknown")
        self.assertEqual(moderation_result["audit_marker"], "kept")
        self.assertEqual(moderation_result["execution_outcome"], "unknown")

    async def test_moderation_task_read_cancel_and_list_are_creator_scoped(self):
        task_a = await self.manager.prepare_moderation(
            "班群", "mute", "1001", "张三", "member", "owner", 600,
            False, "", "qq-main", "A", "origin",
        )
        task_b = await self.manager.prepare_moderation(
            "班群", "kick", "1002", "李四", "member", "owner", 0,
            False, "", "qq-main", "B", "origin",
        )
        self.assertEqual(
            (await self.manager.get_task(task_a["id"], "qq-main", "A"))["id"],
            task_a["id"],
        )
        with self.assertRaisesRegex(ValueError, "群管理任务"):
            await self.manager.get_task(task_a["id"], "qq-main", "B")
        with self.assertRaisesRegex(ValueError, "群管理任务"):
            await self.manager.cancel_task(task_a["id"], "qq-main", "B")
        cancelled = await self.manager.cancel_task(task_a["id"], "qq-main", "A")
        self.assertEqual(cancelled["status"], "CANCELLED")
        own = await self.manager.list_tasks("qq-main", requester_id="B")
        self.assertEqual([task["id"] for task in own], [task_b["id"]])
        admin = await self.manager.list_tasks(
            "qq-main", requester_id="admin", allow_admin_override=True,
        )
        self.assertEqual({task["id"] for task in admin}, {task_a["id"], task_b["id"]})


class HardeningAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_batch_is_one_validated_onebot_message(self):
        self.assertTrue(hasattr(QQAdapter, "send_group_at_member_batch"))
        if not hasattr(QQAdapter, "send_group_at_member_batch"):
            return
        calls = []

        class Client:
            async def call_action(self, action, **kwargs):
                calls.append((action, kwargs))
                return {"status": "ok"}

        class Platform:
            def meta(self):
                return type("Meta", (), {"id": "qq-main", "name": "aiocqhttp"})()

            def get_client(self):
                return Client()

        adapter = QQAdapter(
            type("Context", (), {"get_platform_inst": lambda self, _: Platform()})(),
            "qq-main",
        )
        await adapter.send_group_at_member_batch("123", ["1", "2"], "正文")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "send_group_msg")
        with self.assertRaises(ValueError):
            await adapter.send_group_at_member_batch("123", [])
        with self.assertRaises(ValueError):
            await adapter.send_group_at_member_batch("123", [str(i) for i in range(21)])
