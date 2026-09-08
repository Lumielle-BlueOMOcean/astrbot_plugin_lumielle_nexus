import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import TaskManager
from exporter import export_collection
from storage import Storage


class CoreSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(
            self.storage,
            timezone_name="Asia/Shanghai",
            max_retry_count=3,
        )

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def test_schema_binding_and_reminder_lifecycle(self):
        binding = await self.manager.bind_group(
            alias="班群",
            group_id="123456789",
            platform_id="qq-main",
            created_by="10001",
        )
        self.assertEqual(binding["alias"], "班群")
        self.assertEqual((await self.manager.list_groups("qq-main"))[0]["group_id"], "123456789")

        run_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        task = await self.manager.create_reminder(
            group="班群",
            run_at=run_at,
            message="请提交实验报告",
            mention_all=True,
            platform_id="qq-main",
            creator_id="10001",
            creator_private_origin="aiocqhttp:FriendMessage:10001",
        )
        self.assertTrue(task["id"].startswith("R-"))
        self.assertEqual(json.loads(task["payload"])["mention_all"], True)
        self.assertEqual(len(await self.manager.list_tasks("qq-main")), 1)

        cancelled = await self.manager.cancel_task(task["id"], "qq-main")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(await self.manager.due_tasks(datetime.now(timezone.utc)), [])

    async def test_due_reminder_recovers_after_storage_reopen(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.create_reminder(
            "班群",
            datetime.now(timezone.utc) - timedelta(minutes=1),
            "重载后仍要发送",
            False,
            "qq-main",
            "10001",
            "origin",
        )
        claimed = await self.manager.due_tasks(datetime.now(timezone.utc))
        self.assertEqual(claimed[0]["id"], task["id"])
        self.assertEqual(self.storage.get_task(task["id"])["status"], "PROCESSING")

        self.storage.close()
        reopened_storage = Storage(Path(self.temp_dir.name))
        reopened_manager = TaskManager(reopened_storage, timezone_name="Asia/Shanghai")
        recovered = await reopened_manager.due_tasks(datetime.now(timezone.utc))
        self.assertEqual(recovered[0]["id"], task["id"])
        reopened_storage.close()

    async def test_collection_submission_upserts_and_preserves_partial_data(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.start_collection(
            group="班群",
            title="国庆离校信息统计",
            fields=["姓名", "离校时间", "返校时间"],
            announcement="请按格式填写",
            mention_all=False,
            platform_id="qq-main",
            creator_id="10001",
            creator_private_origin="aiocqhttp:FriendMessage:10001",
        )
        self.assertTrue(task["id"].startswith("C-"))

        partial = await self.manager.process_collection_message(
            platform_id="qq-main",
            group_id="123456789",
            sender_id="20001",
            sender_name="张三",
            raw_message="姓名：张三\n离校时间: 10月1日 14:00",
        )
        self.assertEqual(partial["missing"], ["返校时间"])
        self.assertEqual(len(self.storage.list_entries(task["id"])), 1)

        complete = await self.manager.process_collection_message(
            platform_id="qq-main",
            group_id="123456789",
            sender_id="20001",
            sender_name="张三（新）",
            raw_message="姓名：张三\n离校时间：10月1日 14:00\n返校时间：10月6日",
        )
        self.assertEqual(complete["missing"], [])
        entries = self.storage.list_entries(task["id"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sender_name"], "张三（新）")
        self.assertEqual(json.loads(entries[0]["parsed_data"])["返校时间"], "10月6日")

        with self.assertRaises(ValueError):
            await self.manager.start_collection(
                group="班群",
                title="重复统计",
                fields=["内容"],
                announcement="",
                mention_all=False,
                platform_id="qq-main",
                creator_id="10001",
                creator_private_origin="origin",
            )

    async def test_collection_stop_transitions_and_summary(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.start_collection(
            group="班群",
            title="单字段统计",
            fields=["内容"],
            announcement="",
            mention_all=False,
            platform_id="qq-main",
            creator_id="10001",
            creator_private_origin="origin",
        )
        result = await self.manager.process_collection_message(
            "qq-main", "123456789", "20001", "李四", "已完成",
        )
        self.assertEqual(result["entry"]["parsed_data"], {"内容": "已完成"})
        snapshot = await self.manager.stop_collection(task["id"], "qq-main")
        self.assertEqual(snapshot["task"]["status"], "PROCESSING")
        await self.manager.complete_collection(task["id"], {"export_path": "/tmp/result.xlsx"})
        final = self.storage.get_task(task["id"])
        self.assertEqual(final["status"], "COMPLETED")


class CollectionExportTests(unittest.TestCase):
    def test_export_contains_requested_sheets_and_safe_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "C-20260909-001",
                "group_alias": "班群",
                "group_id": "123456789",
                "creator_id": "10001",
                "created_at": "2026-09-09T01:00:00+00:00",
                "payload": json.dumps({"title": "国庆/离校:统计?", "fields": ["姓名", "离校时间"]}, ensure_ascii=False),
            }
            entries = [
                {
                    "sender_id": "20001",
                    "sender_name": "张三",
                    "raw_message": "姓名：张三\n离校时间：10月1日",
                    "parsed_data": json.dumps({"姓名": "张三", "离校时间": "10月1日"}, ensure_ascii=False),
                    "submitted_at": "2026-09-09T01:01:00+00:00",
                    "updated_at": "2026-09-09T01:02:00+00:00",
                },
            ]
            output = export_collection(Path(temp_dir), task, entries, members=None)
            self.assertTrue(output.exists())
            self.assertNotIn("/", output.name)
            from openpyxl import load_workbook

            workbook = load_workbook(output, read_only=True)
            self.assertEqual(
                workbook.sheetnames,
                ["统计结果", "未提交成员", "任务信息"],
            )
            self.assertEqual(workbook["统计结果"].cell(1, 1).value, "QQ")
            self.assertIn("无法获取完整群成员名单", workbook["未提交成员"].cell(1, 1).value)


class PluginContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_metadata_and_config_contract(self):
        metadata = (self.ROOT / "metadata.yaml").read_text(encoding="utf-8")
        config = json.loads((self.ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("name: astrbot_plugin_lumielle_nexus", metadata)
        self.assertIn('version: "0.1.0"', metadata)
        self.assertIn('astrbot_version: ">=4.28.0,<5"', metadata)
        self.assertIn("- aiocqhttp", metadata)
        self.assertEqual(config["operator_ids"]["default"], [])
        self.assertEqual(config["timezone"]["default"], "Asia/Shanghai")
        self.assertEqual(config["scheduler_interval_seconds"]["default"], 15)
        self.assertEqual(config["max_retry_count"]["default"], 3)
        self.assertTrue(config["collection_ack"]["default"])

    def test_main_contract_has_current_registration_points(self):
        main = (self.ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("register_star", main)
        self.assertNotIn("@register(", main)
        for tool_name in (
            "nexus_bind_group",
            "nexus_list_groups",
            "nexus_create_reminder",
            "nexus_list_tasks",
            "nexus_cancel_task",
            "nexus_start_collection",
            "nexus_collection_status",
            "nexus_stop_collection",
        ):
            self.assertIn(tool_name, main)
        self.assertIn("event_message_type", main)
        self.assertIn("command_group", main)


class QQAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_routes_required_onebot_actions(self):
        from qq_adapter import QQAdapter

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                if action == "get_group_info":
                    return {"group_id": 123, "group_name": "班群", "member_count": 2}
                if action == "get_group_member_list":
                    return [{"user_id": 20001, "nickname": "张三", "card": ""}]
                return {"message_id": "ok"}

        client = FakeClient()
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp", id="qq-main"),
            get_client=lambda: client,
        )
        context = SimpleNamespace(get_platform_inst=lambda platform_id: platform)
        adapter = QQAdapter(context, "qq-main")

        self.assertEqual((await adapter.get_group_info("123"))["group_name"], "班群")
        self.assertEqual(len(await adapter.get_group_member_list("123")), 1)
        await adapter.send_group_text("123", "通知")
        await adapter.send_group_at_all("123", "开始")
        await adapter.send_private_message("10001", "结果")
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "get_group_info",
                "get_group_member_list",
                "send_group_msg",
                "send_group_msg",
                "send_private_msg",
            ],
        )

    async def test_adapter_converts_protocol_error(self):
        from qq_adapter import QQAdapter, QQAdapterError

        class BrokenClient:
            async def call_action(self, action, **kwargs):
                raise RuntimeError("network detail")

        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp", id="qq-main"),
            get_client=lambda: BrokenClient(),
        )
        context = SimpleNamespace(get_platform_inst=lambda platform_id: platform)
        adapter = QQAdapter(context, "qq-main")
        with self.assertRaises(QQAdapterError) as raised:
            await adapter.get_group_info("123")
        self.assertIn("OneBot 调用失败", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
