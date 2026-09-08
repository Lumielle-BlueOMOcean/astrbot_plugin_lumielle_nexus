import json
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import TaskManager, collection_member_stats, format_local_time
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

    async def test_single_field_collection_requires_structured_submission(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.start_collection(
            "班群", "是否参加", ["是否参加"], "", False,
            "qq-main", "10001", "origin",
        )

        self.assertIsNone(
            await self.manager.process_collection_message(
                "qq-main", "123456789", "20001", "张三", "今天作业好多",
            ),
        )
        self.assertEqual(self.storage.list_entries(task["id"]), [])

        result = await self.manager.process_collection_message(
            "qq-main", "123456789", "20001", "张三", "是否参加：是",
        )
        self.assertEqual(result["entry"]["parsed_data"], {"是否参加": "是"})

    async def test_collection_announcement_failure_can_be_marked_failed_and_retried(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.start_collection(
            "班群", "启动公告失败", ["内容"], "", False,
            "qq-main", "10001", "origin",
        )
        failed = await self.manager.fail_collection(task["id"], "群启动通知发送失败：网络错误")
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["last_error"], "群启动通知发送失败：网络错误")
        self.assertIsNotNone(failed["finished_at"])

        retry = await self.manager.start_collection(
            "班群", "重新启动", ["内容"], "", False,
            "qq-main", "10001", "origin",
        )
        self.assertEqual(retry["status"], "ACTIVE")

    async def test_cancel_active_collection_is_rejected(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.start_collection(
            "班群", "不能取消", ["内容"], "", False,
            "qq-main", "10001", "origin",
        )
        with self.assertRaisesRegex(ValueError, "不能通过 cancel 结束"):
            await self.manager.cancel_task(task["id"], "qq-main")
        self.assertEqual(self.storage.get_task(task["id"])["status"], "ACTIVE")

    async def test_collection_status_rejects_reminder_task(self):
        await self.manager.bind_group("班群", "123456789", "qq-main", "10001")
        task = await self.manager.create_reminder(
            "班群", datetime.now(timezone.utc) + timedelta(minutes=5), "提醒", False,
            "qq-main", "10001", "origin",
        )
        with self.assertRaisesRegex(ValueError, "不是信息收集任务"):
            await self.manager.collection_status(task["id"], "qq-main")

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
            "qq-main", "123456789", "20001", "李四", "内容：已完成",
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

    def test_export_filters_bots_and_uses_configured_timezone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "C-20260909-002",
                "group_alias": "班群",
                "group_id": "123456789",
                "creator_id": "10001",
                "created_at": "2026-09-09T07:00:00+00:00",
                "finished_at": "2026-09-09T08:00:00+00:00",
                "payload": json.dumps({"title": "时区统计", "fields": ["内容"]}),
            }
            entries = [
                {
                    "sender_id": "20001",
                    "sender_name": "张三",
                    "raw_message": "内容：已提交",
                    "parsed_data": json.dumps({"内容": "已提交"}),
                    "submitted_at": "2026-09-09T07:30:00+00:00",
                    "updated_at": "2026-09-09T07:31:00+00:00",
                },
            ]
            members = [
                {"user_id": "99999", "nickname": "机器人自身", "is_robot": False},
                {"user_id": "88888", "nickname": "NapCat机器人", "is_robot": True},
                {"user_id": "20001", "nickname": "张三"},
                {"user_id": "20002", "nickname": "李四"},
            ]
            output = export_collection(
                Path(temp_dir), task, entries, members,
                self_id="99999", timezone_name="Asia/Shanghai",
            )
            from openpyxl import load_workbook

            workbook = load_workbook(output, read_only=True, data_only=False)
            result_sheet = workbook["统计结果"]
            self.assertEqual(result_sheet.cell(2, 4).value, "2026-09-09 15:30:00")
            self.assertEqual(result_sheet.cell(2, 5).value, "2026-09-09 15:31:00")
            missing_sheet = workbook["未提交成员"]
            missing_rows = list(missing_sheet.iter_rows(min_row=2, values_only=True))
            self.assertEqual(missing_rows, [("20002", "李四")])
            info_sheet = workbook["任务信息"]
            info_values = [row[1] for row in info_sheet.iter_rows(min_row=2, values_only=True)]
            self.assertIn("2026-09-09 15:00:00", info_values)
            self.assertIn("2026-09-09 16:00:00", info_values)

    def test_export_keeps_formula_like_inputs_as_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "C-20260909-003",
                "group_alias": "=1+1",
                "group_id": "123456789",
                "creator_id": "10001",
                "created_at": "2026-09-09T07:00:00+00:00",
                "payload": json.dumps({"title": "@SUM(1,1)", "fields": ["+1+1"]}),
            }
            entries = [
                {
                    "sender_id": "20001",
                    "sender_name": "-1+1",
                    "raw_message": "=1+1",
                    "parsed_data": json.dumps({"+1+1": "@SUM(1,1)"}),
                    "submitted_at": "2026-09-09T07:01:00+00:00",
                    "updated_at": "2026-09-09T07:02:00+00:00",
                },
            ]
            output = export_collection(Path(temp_dir), task, entries, members=None)
            from openpyxl import load_workbook

            workbook = load_workbook(output, read_only=True, data_only=False)
            values = [
                workbook["统计结果"].cell(1, 3).value,
                workbook["统计结果"].cell(2, 2).value,
                workbook["统计结果"].cell(2, 3).value,
                workbook["统计结果"].cell(2, 6).value,
                workbook["任务信息"].cell(3, 2).value,
            ]
            for value in values:
                self.assertIsInstance(value, str)
                self.assertNotEqual(value[:1], "=")
                self.assertNotEqual(value[:1], "+")
                self.assertNotEqual(value[:1], "-")
                self.assertNotEqual(value[:1], "@")

    def test_member_stats_exclude_bot_and_robot(self):
        members = [
            {"user_id": "99999", "is_robot": False},
            {"user_id": "88888", "is_robot": True},
            {"user_id": "20001", "nickname": "张三"},
            {"user_id": "20002", "nickname": "李四"},
        ]
        entries = [{"sender_id": "20001"}, {"sender_id": "77777"}]
        stats = collection_member_stats(members, entries, self_id="99999")
        self.assertEqual(stats["eligible_ids"], {"20001", "20002"})
        self.assertEqual(stats["submitted_ids"], {"20001"})
        self.assertEqual(stats["missing_ids"], {"20002"})

    def test_export_time_formatter_converts_utc_to_configured_timezone(self):
        self.assertEqual(
            format_local_time("2026-09-09T07:00:00+00:00", "Asia/Shanghai", "minutes"),
            "2026-09-09 15:00",
        )


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
        self.assertNotIn("event.stop_event()", main)
        self.assertIn("尚未执行的一次性提醒", main)
        self.assertIn("at-least-once", (self.ROOT / "README.md").read_text(encoding="utf-8"))

    def test_package_style_core_import_uses_package_storage(self):
        package_name = "data.plugins.astrbot_plugin_lumielle_nexus"
        package_module = type(sys)(package_name)
        package_module.__path__ = [str(self.ROOT)]
        plugins_module = type(sys)("data.plugins")
        plugins_module.__path__ = [str(self.ROOT.parent)]
        data_module = type(sys)("data")
        data_module.__path__ = [str(self.ROOT.parent)]
        previous = {
            name: sys.modules.get(name)
            for name in ("data", "data.plugins", package_name, f"{package_name}.storage", f"{package_name}.core")
        }
        try:
            sys.modules.update({
                "data": data_module,
                "data.plugins": plugins_module,
                package_name: package_module,
            })
            for name in (f"{package_name}.storage", f"{package_name}.core"):
                sys.modules.pop(name, None)
            storage_spec = importlib.util.spec_from_file_location(
                f"{package_name}.storage", self.ROOT / "storage.py",
            )
            storage_module = importlib.util.module_from_spec(storage_spec)
            sys.modules[f"{package_name}.storage"] = storage_module
            storage_spec.loader.exec_module(storage_module)
            core_spec = importlib.util.spec_from_file_location(
                f"{package_name}.core", self.ROOT / "core.py",
            )
            core_module = importlib.util.module_from_spec(core_spec)
            sys.modules[f"{package_name}.core"] = core_module
            core_spec.loader.exec_module(core_module)
            self.assertIs(core_module.Storage, storage_module.Storage)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


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

    async def test_small_file_upload_uses_base64_uri(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                return {"status": "ok"}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "result.xlsx"
            path.write_bytes(b"small xlsx payload")
            client = FakeClient()
            platform = SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp", id="qq-main"),
                get_client=lambda: client,
            )
            context = SimpleNamespace(get_platform_inst=lambda platform_id: platform)
            from qq_adapter import QQAdapter

            await QQAdapter(context, "qq-main").upload_private_file("10001", path)
            action, kwargs = client.calls[0]
            self.assertEqual(action, "upload_private_file")
            self.assertTrue(kwargs["file"].startswith("base64://"))
            self.assertNotIn(str(path), kwargs["file"])


if __name__ == "__main__":
    unittest.main()
