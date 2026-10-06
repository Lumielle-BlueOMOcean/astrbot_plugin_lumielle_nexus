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
from core import Storage, TaskManager, collection_member_stats


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

    def _group_listener(self, main_module):
        plugin = object.__new__(main_module.LumielleNexus)
        plugin.manager = self.manager
        plugin.storage = self.storage
        plugin.collection_ack = False
        plugin._handle_poll_message = mock.AsyncMock(return_value={"handled": False})
        return plugin

    @staticmethod
    def _group_event(message, timestamp, message_id="timestamp-message"):
        class Event:
            unified_msg_origin = "aiocqhttp:GroupMessage:123456789"

            def __init__(self):
                self.message = message
                self.message_obj = types.SimpleNamespace(
                    timestamp=timestamp, message_id=message_id,
                )

            def get_sender_id(self):
                return "1001"

            def get_sender_name(self):
                return "张三"

            def get_self_id(self):
                return "9000"

            def get_group_id(self):
                return "123456789"

            def get_platform_id(self):
                return "qq-main"

            def get_message_str(self):
                return self.message

            def plain_result(self, value):
                return value

        return Event()

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

            async def get_group_msg_history(self, *_args, **_kwargs):
                return []

            async def send_group_at_member_batch(self, group_id, user_ids, text):
                self.calls.append((group_id, list(user_ids), text))

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = types.SimpleNamespace()
        plugin.manager = self.manager
        plugin.storage = self.storage
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

    async def test_first_no_data_is_provisional_and_recheckable(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({
            "members": [{"user_id": "1001", "status": "no_data", "items": []}],
        }, message="我昨天晚上已经回学校了")
        self.assertEqual(result["rejected"], [])
        self.assertEqual(saved["analysis_cursor_id"], captured["id"])
        self.assertFalse(saved["analysis_incomplete"])
        row = self.storage.list_workflow_messages(task["id"])[0]
        self.assertEqual(row["analysis_state"], "no_data")
        self.assertEqual(row["message_text"], "我昨天晚上已经回学校了")
        self.assertIsNone(self.storage.get_entry(task["id"], "1001"))

        recheck = await self.manager.prepare_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "chase",
        )
        self.assertEqual([item["id"] for item in recheck["messages"]], [captured["id"]])
        self.assertEqual(recheck["messages_by_user"]["1001"], ["我昨天晚上已经回学校了"])
        claimed = self.storage.list_workflow_messages(task["id"])[0]
        self.assertEqual(claimed["no_data_recheck_attempts"], 1)

    async def test_chase_recheck_recovers_no_data_false_negative(self):
        main_module = _load_main_module()
        now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=5)
        collection = await self.manager.start_collection(
            "班群", "返校统计", ["是否返校"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", chase_at=now + timedelta(minutes=10),
            deadline=now + timedelta(hours=1),
            field_value_mappings={"是否返校": {"已返校": ["已返校"], "未返校": ["未返校"]}},
            now=now,
        )
        await self.manager.capture_collection_message(
            collection["id"], "1001", "张三", "我昨天晚上已经回学校了",
            source_message_id="false-negative-reply", now=now + timedelta(seconds=1),
        )

        class Context:
            def __init__(self):
                self.responses = [
                    {"members": [{"user_id": "1001", "status": "no_data", "items": []}]},
                    {"members": [{"user_id": "1001", "status": "ok", "items": [{
                        "field": "是否返校", "value": "已返校",
                        "evidence": "已经回学校了", "confidence": 0.99,
                    }]}]},
                ]
                self.calls = 0

            async def llm_generate(self, **_kwargs):
                self.calls += 1
                return types.SimpleNamespace(
                    completion_text=json.dumps(self.responses.pop(0), ensure_ascii=False),
                )

        class FakeAdapter:
            sent_batches = []

            def __init__(self, *_args):
                pass

            async def get_group_member_list(self, _group_id):
                return [
                    {"user_id": "1001", "nickname": "张三"},
                    {"user_id": "1002", "nickname": "李四"},
                    {"user_id": "9000", "nickname": "Bot"},
                ]

            async def send_group_at_member_batch(self, group_id, user_ids, text):
                self.sent_batches.append((group_id, list(user_ids), text))

        context = Context()
        plugin = self._group_listener(main_module)
        plugin.context = context
        plugin._reconcile_collection_history = mock.AsyncMock(return_value=(
            FakeAdapter(), "9000", {"coverage_status": "FULL"},
        ))
        await plugin._run_collection_checkpoint(
            collection["id"], now + timedelta(minutes=1), "manual",
        )
        self.assertEqual(
            self.storage.list_workflow_messages(collection["id"])[0]["analysis_state"],
            "no_data",
        )
        chase = next(
            child for child in self.storage.list_children(collection["id"])
            if json.loads(child["payload"]).get("checkpoint_type") == "chase"
        )
        chase_payload = json.loads(chase["payload"])
        chase_payload["scheduled_run_at"] = (now + timedelta(minutes=2)).isoformat()
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            await plugin._execute_collection_chase(chase, chase_payload)

        entry = self.storage.get_entry(collection["id"], "1001")
        self.assertEqual(json.loads(entry["parsed_data"]), {"是否返校": "已返校"})
        self.assertEqual(context.calls, 2)
        self.assertTrue(FakeAdapter.sent_batches)
        self.assertNotIn("1001", [
            user_id for _group_id, user_ids, _text in FakeAdapter.sent_batches
            for user_id in user_ids
        ])

    async def test_second_no_data_is_bounded_and_finalize_can_apply_default(self):
        main_module = _load_main_module()
        now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=5)
        collection = await self.manager.start_collection(
            "班群", "返校统计", ["是否返校"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", missing_default_field="是否返校",
            missing_default_value="未返校",
            field_value_mappings={"是否返校": {"已返校": ["已返校"], "未返校": ["未返校"]}},
            now=now,
        )
        await self.manager.capture_collection_message(
            collection["id"], "1001", "张三", "今晚吃什么",
            source_message_id="irrelevant-reply", now=now + timedelta(seconds=1),
        )

        class Context:
            calls = 0

            async def llm_generate(self, **_kwargs):
                self.calls += 1
                return types.SimpleNamespace(completion_text=json.dumps({
                    "members": [{"user_id": "1001", "status": "no_data", "items": []}],
                }))

        class FakeAdapter:
            def __init__(self, *_args):
                pass

            async def get_group_member_list(self, _group_id):
                return [{"user_id": "1001", "nickname": "张三"},
                        {"user_id": "9000", "nickname": "Bot"}]

            async def get_login_info(self):
                return {"user_id": "9000"}

        context = Context()
        plugin = self._group_listener(main_module)
        plugin.context = context
        plugin._reconcile_collection_history = mock.AsyncMock(return_value=(
            FakeAdapter(), "9000", {"coverage_status": "FULL"},
        ))
        await plugin._run_collection_checkpoint(
            collection["id"], now + timedelta(minutes=1), "manual",
        )
        stopped = await self.manager.stop_collection(collection["id"], "qq-main")
        cutoff = self.manager._task_result(stopped["task"])["processing_cutoff"]
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            await plugin._execute_collection_finalize(
                {"id": "finalize", "platform_id": "qq-main", "run_at": cutoff},
                {"collection_task_id": collection["id"], "manual_stop": True,
                 "scheduled_run_at": cutoff},
            )

        self.assertEqual(context.calls, 2)
        self.assertEqual(self.storage.get_task(collection["id"])["status"], "COMPLETED")
        entry = self.storage.get_entry(collection["id"], "1001")
        self.assertEqual(json.loads(entry["parsed_data"]), {"是否返校": "未返校"})
        row = self.storage.list_workflow_messages(collection["id"])[0]
        self.assertEqual(row["analysis_state"], "no_data")
        self.assertEqual(row["no_data_recheck_attempts"], 1)

    async def test_ambiguous_no_data_recheck_keeps_finalize_processing(self):
        main_module = _load_main_module()
        now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=5)
        collection = await self.manager.start_collection(
            "班群", "返校统计", ["是否返校"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", missing_default_field="是否返校",
            missing_default_value="未返校", now=now,
        )
        await self.manager.capture_collection_message(
            collection["id"], "1001", "张三", "我应该已经回去了吧",
            source_message_id="ambiguous-after-no-data", now=now + timedelta(seconds=1),
        )

        class Context:
            def __init__(self):
                self.responses = [
                    {"members": [{"user_id": "1001", "status": "no_data", "items": []}]},
                    {"members": [{"user_id": "1001", "status": "ambiguous", "items": []}]},
                ]
                self.calls = 0

            async def llm_generate(self, **_kwargs):
                self.calls += 1
                response = self.responses.pop(0) if self.responses else {
                    "members": [{"user_id": "1001", "status": "no_data", "items": []}],
                }
                return types.SimpleNamespace(completion_text=json.dumps(response))

        class FakeAdapter:
            def __init__(self, *_args):
                pass

            async def get_group_member_list(self, _group_id):
                raise AssertionError(
                    "ambiguous evidence must stop before member/default processing",
                )

        context = Context()
        plugin = self._group_listener(main_module)
        plugin.context = context
        plugin._reconcile_collection_history = mock.AsyncMock(return_value=(
            FakeAdapter(), "9000", {"coverage_status": "FULL"},
        ))
        await plugin._run_collection_checkpoint(
            collection["id"], now + timedelta(minutes=1), "manual",
        )
        stopped = await self.manager.stop_collection(collection["id"], "qq-main")
        cutoff = self.manager._task_result(stopped["task"])["processing_cutoff"]
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            for task_id in ("finalize-1", "finalize-2"):
                await plugin._execute_collection_finalize(
                    {"id": task_id, "platform_id": "qq-main", "run_at": cutoff},
                    {"collection_task_id": collection["id"], "manual_stop": True,
                     "scheduled_run_at": cutoff},
                )

        row = self.storage.list_workflow_messages(collection["id"])[0]
        self.assertEqual(context.calls, 2)
        self.assertEqual(self.storage.get_task(collection["id"])["status"], "PROCESSING")
        self.assertEqual(row["analysis_state"], "error")
        self.assertEqual(row["no_data_recheck_attempts"], 1)
        self.assertEqual(row["message_text"], "我应该已经回去了吧")
        self.assertIsNone(self.storage.get_entry(collection["id"], "1001"))

    async def test_delayed_event_timestamp_before_deadline_is_preserved(self):
        main_module = _load_main_module()
        processing_at = datetime.now(timezone.utc).replace(microsecond=0)
        deadline = processing_at - timedelta(seconds=1)
        event_at = deadline - timedelta(seconds=1)
        collection = await self.manager.start_collection(
            "班群", "返校", ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=event_at - timedelta(minutes=1),
            deadline=deadline,
        )
        plugin = self._group_listener(main_module)
        event = self._group_event("我刚回来了", event_at.timestamp())
        with mock.patch.object(core_module.TaskManager, "_now_utc", return_value=processing_at):
            _ = [item async for item in plugin.on_group_message(event)]

        workflow = self.storage.list_workflow_messages(collection["id"])
        archived = self.storage.search_group_messages(
            "qq-main", "123456789", "", None, None, 10,
        )
        expected = event_at.isoformat(timespec="seconds")
        self.assertEqual(workflow[0]["sent_at"], expected)
        self.assertEqual(archived[0]["sent_at"], expected)

    async def test_late_event_timestamp_is_rejected_even_if_handler_clock_is_early(self):
        main_module = _load_main_module()
        deadline = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)
        processing_at = deadline - timedelta(seconds=1)
        event_at = deadline + timedelta(seconds=1)
        collection = await self.manager.start_collection(
            "班群", "返校", ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=deadline - timedelta(minutes=1),
            deadline=deadline,
        )
        plugin = self._group_listener(main_module)
        event = self._group_event("我刚回来了", str(event_at.timestamp()))
        with mock.patch.object(core_module.TaskManager, "_now_utc", return_value=processing_at):
            _ = [item async for item in plugin.on_group_message(event)]

        self.assertEqual(self.storage.list_workflow_messages(collection["id"]), [])

    async def test_missing_event_timestamp_falls_back_to_processing_time(self):
        main_module = _load_main_module()
        processing_at = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertIsNone(main_module.LumielleNexus._event_sent_at(
            self._group_event("无效时间戳消息", "not-a-unix-time"),
        ))
        collection = await self.manager.start_collection(
            "班群", "返校", ["返校时间"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1", now=processing_at - timedelta(minutes=1),
        )
        plugin = self._group_listener(main_module)
        event = self._group_event("我刚回来了", None)
        with mock.patch.object(core_module.TaskManager, "_now_utc", return_value=processing_at):
            _ = [item async for item in plugin.on_group_message(event)]

        workflow = self.storage.list_workflow_messages(collection["id"])
        archived = self.storage.search_group_messages(
            "qq-main", "123456789", "", None, None, 10,
        )
        expected = processing_at.isoformat(timespec="seconds")
        self.assertEqual(workflow[0]["sent_at"], expected)
        self.assertEqual(archived[0]["sent_at"], expected)

    async def test_ok_empty_items_is_unresolved_and_keeps_cursor(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({
            "members": [{"user_id": "1001", "status": "ok", "items": []}],
        })
        self.assertEqual(saved["analysis_cursor_id"], 0)
        self.assertTrue(saved["analysis_incomplete"])
        self.assertEqual(
            saved["last_checkpoint"]["rejection_counts"]["empty_ok_result"], 1,
        )

    async def test_empty_ok_validator_marks_sender_unresolved(self):
        validation = core_module.validate_collection_checkpoint_candidate(
            {"members": [{"user_id": "1001", "status": "ok", "items": []}]},
            ["返校时间"],
            {"1001": ["我大概7号回来"]},
        )
        self.assertNotIn("1001", validation["resolved_user_ids"])
        self.assertIn("1001", validation["unresolved_user_ids"])
        self.assertIn(
            {"user_id": "1001", "reason": "empty_ok_result"},
            validation["rejected"],
        )

    async def test_valid_ok_result_is_terminal_and_advances_cursor(self):
        task, captured, result, saved = await self._checkpoint_with_candidate({
            "members": [{
                "user_id": "1001", "status": "ok", "items": [{
                    "field": "返校时间", "value": "7号", "evidence": "7号",
                    "confidence": 0.99,
                }],
            }],
        }, message="我7号回来")
        self.assertEqual(result["rejected"], [])
        self.assertEqual(saved["analysis_cursor_id"], captured["id"])
        self.assertFalse(saved["analysis_incomplete"])

    async def test_confirmation_entry_survives_checkpoint_chase_and_finalize(self):
        main_module = _load_main_module()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        collection = await self.manager.start_collection(
            "班群", "回执确认", ["收到确认"], "", False,
            "qq-main", "operator", "origin",
            chase_at=now + timedelta(minutes=10),
            deadline=now + timedelta(minutes=20),
            missing_default_field="收到确认",
            missing_default_value="未收到",
            now=now,
        )
        captured = await self.manager.capture_collection_message(
            collection["id"], "1001", "张三", "收到", sent_at=now + timedelta(seconds=1),
            now=now + timedelta(seconds=1),
        )
        self.assertEqual(captured["entry"]["parsed_data"], {"收到确认": "已收到"})

        children = self.storage.list_children(collection["id"])
        chase = next(
            child for child in children
            if json.loads(child["payload"]).get("checkpoint_type") == "chase"
        )
        finalize = next(
            child for child in children
            if json.loads(child["payload"]).get("kind") == "collection_finalize"
        )

        class FakeAdapter:
            sent_batches = []

            def __init__(self, _context, _platform_id):
                pass

            async def get_group_member_list(self, _group_id):
                return [
                    {"user_id": "1001", "nickname": "张三"},
                    {"user_id": "9000", "nickname": "Bot"},
                ]

            async def get_login_info(self):
                return {"user_id": "9000"}

            async def get_group_msg_history(self, *_args, **_kwargs):
                return []

            async def send_group_at_member_batch(self, group_id, user_ids, text):
                self.sent_batches.append((group_id, list(user_ids), text))

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = types.SimpleNamespace()
        plugin.manager = self.manager
        plugin.storage = self.storage
        plugin.data_dir = Path(self.temp_dir.name)
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            await plugin._execute_collection_checkpoint(
                chase, json.loads(chase["payload"]),
            )
            await plugin._execute_collection_finalize(
                finalize, json.loads(finalize["payload"]),
            )

        final_status = await self.manager.collection_status(collection["id"], "qq-main")
        entry = final_status["entries"][0]
        self.assertEqual(json.loads(entry["parsed_data"]), {"收到确认": "已收到"})
        members = await FakeAdapter(None, None).get_group_member_list("123456789")
        stats = collection_member_stats(members, final_status["entries"], self_id="9000")
        self.assertNotIn("1001", stats["missing_ids"])
        self.assertEqual(FakeAdapter.sent_batches, [])
        self.assertNotEqual(json.loads(entry["parsed_data"]).get("收到确认"), "未收到")

    async def test_history_reconciliation_resolves_return_reply_before_chase_and_finalize(self):
        main_module = _load_main_module()
        now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=20)
        collection = await self.manager.start_collection(
            "班群", "返校统计", ["是否返校"], "", False,
            "qq-main", "operator", "origin", ai_extraction=True,
            ai_provider_id="provider-1",
            chase_at=now + timedelta(minutes=10),
            deadline=now + timedelta(hours=1),
            missing_default_field="是否返校",
            missing_default_value="未返校",
            auto_export=True,
            field_value_mappings={"是否返校": {"已返校": ["已返校"], "未返校": ["未返校"]}},
            now=now,
        )
        chase = next(
            child for child in self.storage.list_children(collection["id"])
            if json.loads(child["payload"]).get("checkpoint_type") == "chase"
        )
        reply_at = now + timedelta(seconds=5)
        messages = [{
            "message_id": "history-return-a",
            "message_seq": 200,
            "time": reply_at.timestamp(),
            "sender": {"user_id": "1001", "nickname": "A", "card": ""},
            "raw_message": "已返校",
        }]

        class FakeAdapter:
            sent_batches = []
            uploaded = []

            def __init__(self, _context, _platform_id):
                pass

            async def get_group_msg_history(self, _group_id, message_seq=0, reverse_order=True):
                return messages if message_seq == 0 else []

            async def get_login_info(self):
                return {"user_id": "9000"}

            async def get_group_member_list(self, _group_id):
                return [
                    {"user_id": "1001", "nickname": "A"},
                    {"user_id": "1002", "nickname": "B"},
                    {"user_id": "1003", "nickname": "C"},
                    {"user_id": "9000", "nickname": "Bot"},
                ]

            async def send_group_at_member_batch(self, group_id, user_ids, text):
                self.sent_batches.append((group_id, list(user_ids), text))

            async def upload_private_file(self, user_id, path):
                self.uploaded.append((user_id, Path(path)))

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = types.SimpleNamespace()
        plugin.manager = self.manager
        plugin.storage = self.storage
        plugin.data_dir = Path(self.temp_dir.name)
        chase_payload = json.loads(chase["payload"])
        chase_payload["scheduled_run_at"] = datetime.now(timezone.utc).replace(
            microsecond=0,
        ).isoformat(timespec="seconds")
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            await plugin._execute_collection_chase(chase, chase_payload)

            self.assertEqual(len(FakeAdapter.sent_batches), 1)
            self.assertEqual(FakeAdapter.sent_batches[0][1], ["1002", "1003"])
            self.assertNotIn("1001", FakeAdapter.sent_batches[0][1])
            entry = self.storage.get_entry(collection["id"], "1001")
            self.assertEqual(json.loads(entry["parsed_data"]), {"是否返校": "已返校"})
            diagnostics = json.loads(self.storage.get_task(collection["id"])["result"])
            self.assertEqual(diagnostics["history_coverage"], "FULL")
            self.assertEqual(diagnostics["last_chase"]["mentioned_user_ids"], ["1002", "1003"])

            processing = await self.manager.stop_collection(collection["id"], "qq-main")
            await self.manager.record_collection_diagnostics(
                collection["id"], result_fields={"manual_stop": True, "force_export": True},
            )
            cutoff = self.manager._task_result(processing["task"])["processing_cutoff"]
            await plugin._execute_collection_finalize(
                {"id": "manual-finalize", "platform_id": "qq-main", "run_at": cutoff},
                {
                    "collection_task_id": collection["id"], "manual_stop": True,
                    "force_export": True, "scheduled_run_at": cutoff,
                },
            )

        final = await self.manager.collection_status(collection["id"], "qq-main")
        final_entry = self.storage.get_entry(collection["id"], "1001")
        self.assertEqual(final["task"]["status"], "COMPLETED")
        self.assertEqual(json.loads(final_entry["parsed_data"]), {"是否返校": "已返校"})
        self.assertNotEqual(json.loads(final_entry["parsed_data"])["是否返校"], "未返校")
        self.assertEqual(len(FakeAdapter.uploaded), 1)
        self.assertTrue(FakeAdapter.uploaded[0][1].exists())

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

    async def test_invalid_provider_status_is_repaired_once_then_resolved(self):
        main_module = _load_main_module()
        task = await self._ai_task()
        await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我7号回来", source_message_id="repair-success",
        )

        class Context:
            def __init__(self):
                self.responses = [
                    {"members": [{"user_id": "1001", "status": "success", "items": []}]},
                    {"members": [{"user_id": "1001", "status": "ok", "items": [{
                        "field": "返校时间", "value": "7号", "evidence": "7号", "confidence": 0.99,
                    }]}]},
                ]
                self.calls = []

            async def llm_generate(self, **kwargs):
                self.calls.append(kwargs)
                return types.SimpleNamespace(
                    completion_text=json.dumps(self.responses.pop(0), ensure_ascii=False),
                )

        context = Context()
        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = context
        plugin.manager = self.manager
        plugin.storage = self.storage
        result = await plugin._run_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        self.assertEqual(len(context.calls), 2)
        self.assertIn("FORMAT REPAIR", context.calls[1]["prompt"])
        self.assertFalse(result["analysis_incomplete"])
        self.assertEqual(self.storage.get_entry(task["id"], "1001")["parsed_data"], '{"返校时间": "7号"}')
        self.assertEqual(self.storage.count_pending_workflow_messages(task["id"]), 0)

    async def test_invalid_status_after_bounded_repair_preserves_evidence(self):
        main_module = _load_main_module()
        task = await self._ai_task()
        captured = await self.manager.capture_collection_message(
            task["id"], "1001", "张三", "我7号回来", source_message_id="repair-failure",
        )

        class Context:
            def __init__(self):
                self.calls = 0

            async def llm_generate(self, **_kwargs):
                self.calls += 1
                return types.SimpleNamespace(completion_text=json.dumps({
                    "members": [{"user_id": "1001", "status": "success", "items": []}],
                }))

        context = Context()
        plugin = object.__new__(main_module.LumielleNexus)
        plugin.context = context
        plugin.manager = self.manager
        plugin.storage = self.storage
        result = await plugin._run_collection_checkpoint(
            task["id"], datetime.now(timezone.utc), "manual",
        )
        saved = json.loads(self.storage.get_task(task["id"])["result"])
        row = self.storage.list_workflow_messages(task["id"])[0]
        self.assertEqual(context.calls, 2)
        self.assertTrue(result["analysis_incomplete"])
        self.assertEqual(result["rejected"][0]["reason"], "invalid_status")
        self.assertEqual(row["message_text"], "我7号回来")
        self.assertEqual(row["analysis_state"], "error")
        self.assertEqual(saved["analysis_cursor_id"], 0)
        self.assertTrue(saved["analysis_incomplete"])

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

    async def test_chase_checkpoint_routes_to_unified_chase_path(self):
        main_module = _load_main_module()

        class FakeManager:
            async def collection_status(self, _task_id, _platform_id):
                return {"task": {
                    "id": "C-1", "status": "ACTIVE", "payload": json.dumps({}),
                }}

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.manager = FakeManager()
        plugin._execute_collection_chase = mock.AsyncMock()
        await plugin._execute_collection_checkpoint(
            {"platform_id": "qq-main", "run_at": "2026-09-17T10:00:00+00:00"},
            {"collection_task_id": "C-1", "checkpoint_type": "chase"},
        )
        plugin._execute_collection_chase.assert_awaited_once()

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
                self.retries = []

            @staticmethod
            def _task_result(task):
                try:
                    result = json.loads(task.get("result") or "{}")
                except (TypeError, json.JSONDecodeError):
                    result = {}
                return result if isinstance(result, dict) else {}

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

            async def record_collection_diagnostics(self, *_args, **_kwargs):
                return collection

            async def schedule_collection_retry(self, *_args, **kwargs):
                self.retries.append(kwargs)
                return {"id": "R-retry"}

            max_retry_count = 3

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
        plugin.storage = self.storage
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
        self.assertIsNone(manager.completed)
        self.assertEqual(len(manager.retries), 1)


if __name__ == "__main__":
    unittest.main()
