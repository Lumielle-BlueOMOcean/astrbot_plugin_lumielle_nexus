import inspect
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import core as core_module
from core import Storage, TaskManager


def _load_main_module():
    try:
        return importlib.import_module("main")
    except ModuleNotFoundError as exc:
        if exc.name != "astrbot":
            raise

    class _Group:
        def command(self, *_args, **_kwargs):
            return lambda function: function

    class _Filter:
        class EventMessageType:
            GROUP_MESSAGE = "GROUP_MESSAGE"

        def command_group(self, *_args, **_kwargs):
            return lambda _function: _Group()

        def command(self, *_args, **_kwargs):
            return lambda function: function

        def llm_tool(self, *_args, **_kwargs):
            return lambda function: function

        def event_message_type(self, *_args, **_kwargs):
            return lambda function: function

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    api.logger = types.SimpleNamespace(exception=lambda *args, **kwargs: None)
    event.AstrMessageEvent = object
    event.MessageEventResult = object
    event.filter = _Filter()
    star.Context = object

    class _Star:
        def __init__(self, context):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(_name):
            return tempfile.gettempdir()

    star.Star = _Star
    star.StarTools = _StarTools
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
    })
    return importlib.import_module("main")


class CollectionIncidentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(self.storage, timezone_name="Asia/Shanghai")
        await self.manager.bind_group("班群", "123456789", "qq-main", "operator")

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def _ai_task(self, fields=None):
        return await self.manager.start_collection(
            "班群", "返校确认", fields or ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
        )

    async def _checkpoint_with_candidate(self, candidate, message="群里的一条待分析消息"):
        task = await self._ai_task()
        captured = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", message, source_message_id="incident-message",
        )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        result = await self.manager.apply_collection_checkpoint(
            task["id"], snapshot, candidate,
        )
        return task, captured, result, json.loads(self.storage.get_task(task["id"])["result"])

    async def test_new_checkpoint_child_persists_default_message(self):
        now = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        task = await self.manager.start_collection(
            "班群", "返校确认", ["返校时间"], "", False,
            "qq-main", "operator", "origin", chase_at=now + timedelta(minutes=10),
            now=now,
        )
        chase = next(
            child for child in self.storage.list_children(task["id"])
            if json.loads(child["payload"]).get("checkpoint_type") == "chase"
        )
        payload = json.loads(chase["payload"])
        self.assertEqual(payload.get("message"), "请还未完成「返校确认」的同学尽快回复。")

    async def test_legacy_chase_without_message_uses_fallback(self):
        main_module = _load_main_module()
        collection = await self.manager.start_collection(
            "班群", "返校确认", ["返校时间"], "", False,
            "qq-main", "operator", "origin",
        )
        child = await self.manager.schedule_collection_chase(
            collection["id"], datetime.now(timezone.utc), "请提交", 60,
            "qq-main", "operator", "origin",
        )
        payload = json.loads(child["payload"])
        payload.pop("message")
        self.storage._conn.execute(
            "UPDATE tasks SET payload = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), child["id"]),
        )
        self.storage._conn.commit()
        child = self.storage.get_task(child["id"])

        class FakeAdapter:
            calls = []

            def __init__(self, _context, _platform_id):
                pass

            async def get_group_member_list(self, _group_id):
                return [{"user_id": "1001", "nickname": "张三"}]

            async def get_login_info(self):
                return {"user_id": "9000"}

            async def send_group_at_member_batch(self, group_id, user_ids, text):
                self.calls.append((group_id, list(user_ids), text))

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = types.SimpleNamespace()
        plugin.manager = self.manager
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            try:
                await plugin._execute_collection_chase(child, payload)
            except Exception as exc:
                self.fail(f"legacy chase raised unexpectedly: {exc}")
        self.assertEqual(FakeAdapter.calls[0][2], "请还未完成「返校确认」的同学尽快回复。")
        future_children = [
            item for item in self.storage.list_children(collection["id"])
            if item["id"] != child["id"]
        ]
        self.assertEqual(len(future_children), 1)
        self.assertEqual(
            json.loads(future_children[0]["payload"])["message"],
            "请还未完成「返校确认」的同学尽快回复。",
        )

    async def test_confirmation_collection_reply_is_deterministic(self):
        task = await self.manager.start_collection(
            "班群", "回执", ["收到确认"], "", False,
            "qq-main", "operator", "origin",
        )
        result = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "收到",
        )
        self.assertTrue(result["deterministic_handled"])
        self.assertEqual(json.loads(self.storage.get_entry(task["id"], "1001")["parsed_data"]), {
            "收到确认": "已收到",
        })

    async def test_received_is_not_deterministic_for_name_field(self):
        task = await self.manager.start_collection(
            "班群", "姓名", ["姓名"], "", False,
            "qq-main", "operator", "origin",
        )
        result = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "收到",
        )
        self.assertFalse(result["deterministic_handled"])
        self.assertIsNone(self.storage.get_entry(task["id"], "1001"))

    async def test_no_data_is_terminal_and_advances_cursor(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({
            "members": [{"user_id": "1001", "status": "no_data", "items": []}],
        })
        self.assertEqual(result["rejected"], [])
        self.assertEqual(saved["analysis_cursor_id"], captured["id"])
        self.assertFalse(saved["analysis_incomplete"])

    async def test_ambiguous_result_keeps_cursor_and_marks_incomplete(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({
            "members": [{"user_id": "1001", "status": "ambiguous", "items": []}],
        })
        self.assertEqual(saved["analysis_cursor_id"], 0)
        self.assertTrue(saved["analysis_incomplete"])
        self.assertEqual(saved["last_checkpoint"]["rejection_counts"]["ambiguous"], 1)

    async def test_low_confidence_result_keeps_cursor_and_records_reason(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({
            "members": [{
                "user_id": "1001", "status": "ok", "items": [{
                    "field": "返校时间", "value": "7号", "evidence": "7号",
                    "confidence": 0.89,
                }],
            }],
        }, message="我大概7号回来")
        self.assertEqual(saved["analysis_cursor_id"], 0)
        self.assertTrue(saved["analysis_incomplete"])
        self.assertEqual(saved["last_checkpoint"]["rejection_counts"]["low_or_invalid_confidence"], 1)

    async def test_missing_member_result_keeps_cursor(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({"members": []})
        self.assertEqual(saved["analysis_cursor_id"], 0)
        self.assertTrue(saved["analysis_incomplete"])
        self.assertEqual(saved["last_checkpoint"]["rejection_counts"]["missing_member_result"], 1)

    async def test_analysis_error_does_not_advance_cursor(self):
        task = await self._ai_task()
        captured = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我大概7号回来", source_message_id="error-message",
        )
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        await self.manager.advance_collection_checkpoint(
            task["id"], snapshot, analysis_error="provider unavailable",
        )
        saved = json.loads(self.storage.get_task(task["id"])["result"])
        self.assertEqual(saved["analysis_cursor_id"], 0)
        self.assertTrue(saved["analysis_incomplete"])
        self.assertEqual(saved["analysis_error"], "provider unavailable")

    async def test_unresolved_then_successful_retry_advances_and_clears_error(self):
        task, captured, first, saved = await self._checkpoint_with_candidate({
            "members": [{"user_id": "1001", "status": "ambiguous", "items": []}],
        }, message="我大概7号回来")
        self.assertTrue(saved["analysis_incomplete"])
        snapshot = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        second = await self.manager.apply_collection_checkpoint(
            task["id"], snapshot, {"members": [{
                "user_id": "1001", "status": "ok", "items": [{
                    "field": "返校时间", "value": "7号", "evidence": "7号",
                    "confidence": 0.99,
                }],
            }]},
        )
        saved = json.loads(self.storage.get_task(task["id"])["result"])
        self.assertEqual(second["applied"], ["1001"])
        self.assertEqual(saved["analysis_cursor_id"], captured["id"])
        self.assertFalse(saved["analysis_incomplete"])
        self.assertIsNone(saved["analysis_error"])

    async def test_collection_status_defaults_to_read_only(self):
        main_module = _load_main_module()
        self.assertIs(
            inspect.signature(main_module.LumielleNexus._collection_status)
            .parameters["refresh"].default,
            False,
        )
        self.assertIs(
            inspect.signature(main_module.LumielleNexus.nexus_collection_status)
            .parameters["refresh"].default,
            False,
        )

    async def test_incomplete_checkpoint_suppresses_chase(self):
        main_module = _load_main_module()

        class FakeManager:
            async def collection_status(self, _task_id, _platform_id):
                return {"task": {
                    "id": "C-1", "status": "ACTIVE", "payload": json.dumps({}),
                }}

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.manager = FakeManager()
        plugin._run_collection_checkpoint = mock.AsyncMock(return_value={
            "analysis_incomplete": True,
        })
        plugin._execute_collection_chase = mock.AsyncMock()
        await plugin._execute_collection_checkpoint(
            {"platform_id": "qq-main", "run_at": "2026-09-17T10:00:00+00:00"},
            {"collection_task_id": "C-1", "checkpoint_type": "chase"},
        )
        plugin._execute_collection_chase.assert_not_awaited()

    async def test_incomplete_finalize_skips_defaults_and_export(self):
        main_module = _load_main_module()

        collection = {
            "id": "C-1", "status": "ACTIVE", "group_id": "123456789",
            "group_alias": "班群", "platform_id": "qq-main", "creator_id": "operator",
            "payload": json.dumps({
                "missing_default_field": "状态", "missing_default_value": "未收到",
                "auto_export": True, "title": "回执", "fields": ["状态"],
            }),
        }

        class FakeManager:
            def __init__(self):
                self.default_calls = 0
                self.completed = None

            async def collection_status(self, _task_id, _platform_id):
                return {"task": collection, "entries": []}

            async def stop_collection(self, _task_id, _platform_id):
                return {"task": {**collection, "status": "PROCESSING"}, "entries": []}

            async def apply_missing_default(self, *args):
                self.default_calls += 1

            async def list_member_identities(self, *_args):
                return []

            async def complete_collection(self, _task_id, result):
                self.completed = result

        class FakeAdapter:
            upload_calls = 0

            def __init__(self, _context, _platform_id):
                pass

            async def get_group_member_list(self, _group_id):
                return [{"user_id": "1001", "nickname": "张三"}]

            async def get_login_info(self):
                return {"user_id": "9000"}

            async def upload_private_file(self, *_args):
                self.upload_calls += 1

        manager = FakeManager()
        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = types.SimpleNamespace()
        plugin.manager = manager
        plugin.data_dir = Path(self.temp_dir.name)
        plugin._run_collection_checkpoint = mock.AsyncMock(return_value={
            "analysis_incomplete": True,
        })
        with (
            mock.patch.object(main_module, "QQAdapter", FakeAdapter),
            mock.patch.object(main_module, "export_collection") as export_mock,
        ):
            await plugin._execute_collection_finalize(
                {"id": "R-1", "platform_id": "qq-main", "run_at": "2026-09-17T10:00:00+00:00"},
                {"collection_task_id": "C-1", "scheduled_run_at": "2026-09-17T10:00:00+00:00"},
            )
        self.assertEqual(manager.default_calls, 0)
        export_mock.assert_not_called()
        self.assertEqual(manager.completed["auto_export_skipped_reason"], "analysis_incomplete")


if __name__ == "__main__":
    unittest.main()
