import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from poll_service import (
    PollClosedError,
    PollError,
    PollService,
    normalize_poll_reply,
    parse_poll_message,
    validate_semantic_vote_candidate,
)
from storage import Storage


async def _async_value(value):
    return value


def _load_main_module():
    try:
        return importlib.import_module("main")
    except ModuleNotFoundError as exc:
        if exc.name not in {"astrbot"}:
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
    api.logger = types.SimpleNamespace(
        exception=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )
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
    with mock.patch.dict(sys.modules, {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
    }):
        return importlib.import_module("main")


class PollServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.storage.upsert_binding(
            "班群", "123456789", "qq-main", "operator", "2026-09-17T00:00:00+00:00",
        )
        self.service = PollService(self.storage, timezone_name="Asia/Shanghai")

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def _create(self, **overrides):
        values = {
            "group": "班群",
            "title": "明天下午几点开会？",
            "options": ["14点", "15点", "16点"],
            "description": "请按实际情况选择。",
            "deadline": "",
            "multiple_choice": False,
            "max_choices": 0,
            "allow_change": True,
            "auto_publish_result": True,
            "platform_id": "qq-main",
            "creator_id": "operator",
            "now": datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return await self.service.create_poll(**values)

    async def test_create_persists_options_and_reply_keys(self):
        poll = await self._create(reply_keys=["a", "b", "c"])
        self.assertTrue(poll["id"].startswith("P-"))
        self.assertEqual(
            [(item["label"], item["reply_key"]) for item in poll["options"]],
            [("14点", "a"), ("15点", "b"), ("16点", "c")],
        )
        self.assertNotIn("public_token", poll)
        self.assertNotIn("public_url", poll)
        self.assertEqual(self.storage.get_poll(poll["id"])["title"], poll["title"])

    async def test_single_choice_vote_and_result_aggregation(self):
        poll = await self._create(reply_keys=["a", "b", "c"])
        receipt = await self.service.cast_vote(poll["id"], [2], "1001")
        self.assertEqual(receipt["choices"], [2])
        result = await self.service.get_result(poll["id"])
        self.assertEqual(result["participant_count"], 1)
        self.assertEqual([item["votes"] for item in result["options"]], [0, 1, 0])
        self.assertEqual(result["options"][1]["percentage"], 100.0)

    async def test_allow_change_updates_ballot_without_new_participant(self):
        poll = await self._create(allow_change=True)
        await self.service.cast_vote(poll["id"], [1], "1001")
        await self.service.cast_vote(poll["id"], [3], "1001")
        result = await self.service.get_result(poll["id"])
        self.assertEqual(result["participant_count"], 1)
        self.assertEqual([item["votes"] for item in result["options"]], [0, 0, 1])

    async def test_disallow_change_and_multiple_choice_limit(self):
        single = await self._create(allow_change=False)
        await self.service.cast_vote(single["id"], [1], "1001")
        with self.assertRaises(PollError):
            await self.service.cast_vote(single["id"], [2], "1001")

        multiple = await self._create(multiple_choice=True, max_choices=2)
        with self.assertRaises(PollError):
            await self.service.cast_vote(multiple["id"], [1, 2, 3], "1002")
        await self.service.cast_vote(multiple["id"], [1, 3], "1002")

    async def test_expired_vote_closes_poll_and_scheduler_does_not_repeat(self):
        now = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        poll = await self._create(deadline="2026-09-17 19:00", now=now)
        with self.assertRaises(PollClosedError):
            await self.service.cast_vote(
                poll["id"], [1], "1001", now=now + timedelta(hours=9),
            )
        self.assertEqual(self.storage.get_poll(poll["id"])["status"], "CLOSED")
        self.assertEqual(await self.service.close_due_polls(now + timedelta(hours=10)), [])

    async def test_storage_migration_removes_web_columns_and_preserves_rows(self):
        legacy_dir = tempfile.TemporaryDirectory()
        db_path = Path(legacy_dir.name) / "lumielle_nexus.db"
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE polls (
                id TEXT PRIMARY KEY, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                group_alias TEXT NOT NULL, creator_id TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                multiple_choice INTEGER NOT NULL DEFAULT 0, max_choices INTEGER NOT NULL DEFAULT 1,
                allow_change INTEGER NOT NULL DEFAULT 1,
                result_visibility TEXT NOT NULL DEFAULT 'after_close',
                auto_publish_result INTEGER NOT NULL DEFAULT 1, deadline_at TEXT,
                public_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT,
                announcement_sent INTEGER NOT NULL DEFAULT 0,
                result_published INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
            CREATE TABLE poll_options (
                poll_id TEXT NOT NULL, option_id INTEGER NOT NULL, position INTEGER NOT NULL,
                label TEXT NOT NULL, PRIMARY KEY (poll_id, option_id),
                UNIQUE (poll_id, position)
            );
            CREATE TABLE poll_ballots (
                poll_id TEXT NOT NULL, voter_hash TEXT NOT NULL, choices_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (poll_id, voter_hash)
            );
            INSERT INTO polls VALUES
            ('P-20260917-001', 'qq-main', '123456789', '班群', 'operator', '旧投票', '',
             'CLOSED', 0, 1, 1, 'after_close', 1, NULL, 'old-token',
             '2026-09-17T00:00:00+00:00', '2026-09-17T00:00:00+00:00',
             '2026-09-17T01:00:00+00:00', 'manual', 1, 0, NULL);
            INSERT INTO poll_options VALUES
            ('P-20260917-001', 1, 1, '通过');
            INSERT INTO poll_ballots VALUES
            ('P-20260917-001', 'old-browser-hash', '[1]',
             '2026-09-17T00:01:00+00:00', '2026-09-17T00:01:00+00:00');
            """,
        )
        connection.commit()
        connection.close()

        migrated = Storage(Path(legacy_dir.name))
        columns = {
            row[1] for row in migrated._conn.execute("PRAGMA table_info(polls)")
        }
        ballot_columns = {
            row[1] for row in migrated._conn.execute("PRAGMA table_info(poll_ballots)")
        }
        self.assertNotIn("public_token", columns)
        self.assertNotIn("result_visibility", columns)
        self.assertNotIn("voter_hash", ballot_columns)
        self.assertEqual(migrated.get_poll("P-20260917-001")["title"], "旧投票")
        self.assertEqual(migrated.get_poll_options("P-20260917-001")[0]["reply_key"], "1")
        self.assertEqual(
            migrated.list_poll_ballots("P-20260917-001")[0]["voter_id"],
            "legacy-web:old-browser-hash",
        )
        self.assertEqual(
            migrated._conn.execute("PRAGMA foreign_key_check").fetchall(), [],
        )
        migrated.close()
        legacy_dir.cleanup()

    async def test_poll_reply_normalization_and_exact_alias_parsing(self):
        poll = await self._create(reply_keys=["abc", "def", "ghi"])
        self.assertEqual(normalize_poll_reply(" ＡＢＣ "), "abc")
        self.assertEqual(parse_poll_message("abc", poll), [1])
        self.assertEqual(parse_poll_message("2", poll), [2])
        self.assertEqual(parse_poll_message("14点", poll), [1])
        multi = dict(poll)
        multi["multiple_choice"] = True
        multi["max_choices"] = 2
        self.assertEqual(parse_poll_message("abc def", multi), [1, 2])

    async def test_semantic_candidate_validation_requires_strict_evidence(self):
        valid = {
            "status": "vote",
            "choices": [2],
            "confidence": 0.95,
            "evidence": "我选15点",
        }
        self.assertEqual(
            validate_semantic_vote_candidate(valid, 3, False, 1, "我选15点"), [2],
        )
        with self.assertRaises(PollError):
            validate_semantic_vote_candidate(
                {**valid, "confidence": 0.89}, 3, False, 1, "我选15点",
            )
        with self.assertRaises(PollError):
            validate_semantic_vote_candidate(
                {**valid, "evidence": "模型推断"}, 3, False, 1, "我选15点",
            )

    async def test_group_message_deterministic_vote_is_silent_and_uses_qq_id(self):
        main_module = _load_main_module()
        poll = await self._create()

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "2"

            def get_sender_id(self):
                return "1001"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": True, "kind": "success"})
        self.assertEqual(
            self.storage.list_poll_ballots(poll["id"])[0]["voter_id"], "1001",
        )

    async def test_semantic_poll_vote_is_narrowed_and_provider_called_once(self):
        main_module = _load_main_module()
        poll = await self._create(
            options=["周六", "周日"],
            semantic_fallback=True,
            ai_provider_id="provider-1",
        )
        calls = []

        class Context:
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(
                    completion_text='{"status":"vote","choices":[1],"confidence":0.95,"evidence":"周六"}',
                )

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "我觉得周六比较好"

            def get_sender_id(self):
                return "1002"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": True, "kind": "success"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["chat_provider_id"], "provider-1")
        self.assertEqual(self.storage.list_poll_ballots(poll["id"])[0]["voter_id"], "1002")

    async def test_single_open_poll_ordinal_uses_semantic_fallback(self):
        main_module = _load_main_module()
        poll = await self._create(
            options=["14点", "15点", "16点"],
            semantic_fallback=True,
            ai_provider_id="provider-1",
        )
        calls = []

        class Context:
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(
                    completion_text='{"status":"vote","choices":[2],"confidence":0.95,"evidence":"第二个"}',
                )

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "第二个吧"

            def get_sender_id(self):
                return "1005"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": True, "kind": "success"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.storage.list_poll_ballots(poll["id"])[0]["choices_json"], "[2]")

    async def test_explicit_poll_id_uses_semantic_fallback(self):
        main_module = _load_main_module()
        poll = await self._create(
            options=["周六", "周日"],
            semantic_fallback=True,
            ai_provider_id="provider-1",
        )
        calls = []

        class Context:
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(
                    completion_text='{"status":"vote","choices":[2],"confidence":0.95,"evidence":"第二个"}',
                )

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return f"投票 {poll['id']} 第二个吧"

            def get_sender_id(self):
                return "1006"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": True, "kind": "success"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.storage.list_poll_ballots(poll["id"])[0]["choices_json"], "[2]")

    async def test_ordinary_numeric_message_does_not_trigger_poll_llm(self):
        main_module = _load_main_module()
        await self._create(ai_provider_id="provider-1")
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "今天用1号实验室"

            def get_sender_id(self):
                return "1007"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": False, "kind": "no_match"})
        self.assertEqual(calls, [])
        self.assertEqual(self.storage.list_poll_ballots("P-20260917-001"), [])

    async def test_ordinary_substring_message_passes_through(self):
        main_module = _load_main_module()
        await self._create(ai_provider_id="provider-1")
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "投影仪坏了"

            def get_sender_id(self):
                return "1008"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": False, "kind": "no_match"})
        self.assertEqual(calls, [])
        self.assertEqual(self.storage.list_poll_ballots("P-20260917-001"), [])

    async def test_non_explicit_semantic_failure_passes_through(self):
        main_module = _load_main_module()
        await self._create(
            options=["周六", "周日"],
            semantic_fallback=True,
            ai_provider_id="provider-1",
        )
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)
                raise RuntimeError("provider unavailable")

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "我还是选周日吧"

            def get_sender_id(self):
                return "1009"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result, {"handled": False, "kind": "not_vote"})
        self.assertEqual(calls, [True])
        self.assertEqual(self.storage.list_poll_ballots("P-20260917-001"), [])

    async def test_explicit_semantic_failure_returns_short_error(self):
        main_module = _load_main_module()
        poll = await self._create(
            options=["周六", "周日"],
            semantic_fallback=True,
            ai_provider_id="provider-1",
        )
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)
                raise RuntimeError("provider unavailable")

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return f"投票 {poll['id']} 我还是选周日吧"

            def get_sender_id(self):
                return "1010"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertTrue(result["handled"])
        self.assertEqual(result["kind"], "error")
        self.assertIn("没有识别出明确选择", result["message"])
        self.assertEqual(calls, [True])
        self.assertEqual(self.storage.list_poll_ballots(poll["id"]), [])

    async def test_ordinary_group_message_does_not_call_poll_provider(self):
        main_module = _load_main_module()
        await self._create(options=["周六", "周日"], ai_provider_id="provider-1")
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "大家晚上好"

            def get_sender_id(self):
                return "1003"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result["kind"], "no_match")
        self.assertEqual(calls, [])

    async def test_multiple_open_polls_require_explicit_poll_id(self):
        main_module = _load_main_module()
        await self._create(title="第一个投票")
        await self._create(title="第二个投票")

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "1"

            def get_sender_id(self):
                return "1004"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        result = await plugin._handle_poll_message(Event())
        self.assertEqual(result["kind"], "ambiguity")
        self.assertEqual(self.storage.list_poll_ballots("P-20260917-001"), [])

    async def test_single_choice_cardinality_error_is_reported_without_llm(self):
        main_module = _load_main_module()
        poll = await self._create(ai_provider_id="provider-1")
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "1 2"

            def get_sender_id(self):
                return "1011"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertTrue(result["handled"])
        self.assertEqual(result["kind"], "error")
        self.assertIn("单选投票只能选择一个选项", result["message"])
        self.assertEqual(calls, [])
        self.assertEqual(self.storage.list_poll_ballots(poll["id"]), [])

    async def test_max_choices_error_is_reported_without_llm(self):
        main_module = _load_main_module()
        poll = await self._create(
            multiple_choice=True,
            max_choices=2,
            reply_keys=["abc", "def", "ghi"],
            ai_provider_id="provider-1",
        )
        calls = []

        class Context:
            async def llm_generate(self, **_kwargs):
                calls.append(True)

        class Event:
            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_message_str(self):
                return "abc def ghi"

            def get_sender_id(self):
                return "1012"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.storage = self.storage
        plugin.context = Context()
        result = await plugin._handle_poll_message(Event())
        self.assertTrue(result["handled"])
        self.assertEqual(result["kind"], "error")
        self.assertIn("最多选择 2 个选项", result["message"])
        self.assertEqual(calls, [])
        self.assertEqual(self.storage.list_poll_ballots(poll["id"]), [])

    async def test_poll_control_uses_operator_and_group_admin_authorization(self):
        main_module = _load_main_module()

        class Event:
            def __init__(self, sender, private):
                self.sender = sender
                self.private = private

            def is_private_chat(self):
                return self.private

            def is_admin(self):
                return False

            def get_sender_id(self):
                return self.sender

            def get_platform_name(self):
                return "aiocqhttp"

            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.operator_ids = {"operator"}
        self.assertTrue(plugin._authorized_for_control(Event("operator", True))[0])
        self.assertFalse(plugin._authorized_for_control(Event("member", True))[0])
        plugin.manager = types.SimpleNamespace(
            get_binding=lambda *_args: _async_value({"alias": "班群", "group_id": "123456789"}),
        )
        plugin._adapter = lambda _event: types.SimpleNamespace(
            get_group_member_info=lambda *_args: _async_value({"role": "member"}),
        )
        denied = await plugin._authorized_collection_control(Event("member", False), "班群")
        self.assertFalse(denied[0])
        plugin._adapter = lambda _event: types.SimpleNamespace(
            get_group_member_info=lambda *_args: _async_value({"role": "admin"}),
        )
        allowed = await plugin._authorized_collection_control(Event("admin", False), "班群")
        self.assertTrue(allowed[0])

    async def test_auto_publish_result_marks_once(self):
        main_module = _load_main_module()
        poll = await self._create()
        await self.service.cast_vote(poll["id"], [1], "1001")
        await self.service.close_poll(poll["id"], "deadline")

        class FakeAdapter:
            calls = []

            def __init__(self, _context, _platform_id):
                pass

            async def send_group_text(self, group_id, text):
                self.calls.append((group_id, text))

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.context = types.SimpleNamespace()
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            await plugin._publish_poll_result(poll)
            await plugin._publish_poll_result(await self.service.get_poll(poll["id"]))
        self.assertEqual(len(FakeAdapter.calls), 1)
        self.assertTrue(self.storage.get_poll(poll["id"])["result_published"])

    async def test_failed_poll_publication_is_not_retried_by_maintenance(self):
        main_module = _load_main_module()
        poll = await self._create()
        await self.service.close_poll(poll["id"], "manual")

        class FailingAdapter:
            calls = []

            def __init__(self, _context, _platform_id):
                pass

            async def send_group_text(self, _group_id, _text):
                self.calls.append(True)
                raise RuntimeError("transport failed")

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.context = types.SimpleNamespace()
        with mock.patch.object(main_module, "QQAdapter", FailingAdapter):
            await plugin._maintain_polls_once()
            await plugin._maintain_polls_once()

        stored = self.storage.get_poll(poll["id"])
        self.assertEqual(len(FailingAdapter.calls), 1)
        self.assertFalse(stored["result_published"])
        self.assertEqual(stored["last_error"], "transport failed")

    async def test_announcement_error_is_cleared_when_poll_closes(self):
        main_module = _load_main_module()
        poll = await self._create()
        await self.service.record_error(poll["id"], "announcement failed")
        await self.service.close_poll(poll["id"], "manual")
        self.assertIsNone(self.storage.get_poll(poll["id"])["last_error"])

        class FakeAdapter:
            calls = []

            def __init__(self, _context, _platform_id):
                pass

            async def send_group_text(self, group_id, text):
                self.calls.append((group_id, text))

        plugin = object.__new__(main_module.LumielleNexus)
        plugin.poll_service = self.service
        plugin.context = types.SimpleNamespace()
        with mock.patch.object(main_module, "QQAdapter", FakeAdapter):
            await plugin._maintain_polls_once()
            await plugin._maintain_polls_once()
        self.assertEqual(len(FakeAdapter.calls), 1)
        self.assertTrue(self.storage.get_poll(poll["id"])["result_published"])


if __name__ == "__main__":
    unittest.main()
