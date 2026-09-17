import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from storage import Storage

try:
    from poll_service import PollClosedError, PollError, PollService
    from poll_web import PollWeb
except ModuleNotFoundError:
    PollClosedError = PollError = PollService = PollWeb = None


async def _async_value(value):
    return value


def _load_main_module():
    try:
        return importlib.import_module("main")
    except ModuleNotFoundError as exc:
        if exc.name not in {"astrbot", "poll_service", "poll_web"}:
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
        self.service = self._new_service()

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def _new_service(self):
        self.assertIsNotNone(PollService, "PollService is not implemented")
        return PollService(
            self.storage,
            timezone_name="Asia/Shanghai",
            web_enabled=True,
            public_base_url="https://poll.example.com",
        )

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
            "result_visibility": "after_close",
            "auto_publish_result": True,
            "platform_id": "qq-main",
            "creator_id": "operator",
            "now": datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return await self.service.create_poll(**values)

    async def test_create_persists_options_token_and_public_url(self):
        poll = await self._create()
        self.assertTrue(poll["id"].startswith("P-"))
        self.assertEqual([item["label"] for item in poll["options"]], ["14点", "15点", "16点"])
        self.assertEqual(len(poll["public_token"]), 43)
        self.assertEqual(
            poll["public_url"],
            f"https://poll.example.com/poll/{poll['public_token']}",
        )
        self.assertEqual(self.storage.get_poll(poll["id"])["title"], poll["title"])

    async def test_single_choice_vote_and_result_aggregation(self):
        poll = await self._create()
        receipt = await self.service.vote(poll["public_token"], [2], "browser-a")
        self.assertEqual(receipt["choices"], [2])
        result = await self.service.get_result(poll["id"])
        self.assertEqual(result["participant_count"], 1)
        self.assertEqual([item["votes"] for item in result["options"]], [0, 1, 0])
        self.assertEqual(result["options"][1]["percentage"], 100.0)

    async def test_allow_change_updates_ballot_without_new_participant(self):
        poll = await self._create(allow_change=True)
        await self.service.vote(poll["public_token"], [1], "browser-a")
        await self.service.vote(poll["public_token"], [3], "browser-a")
        result = await self.service.get_result(poll["id"])
        self.assertEqual(result["participant_count"], 1)
        self.assertEqual([item["votes"] for item in result["options"]], [0, 0, 1])

    async def test_disallow_change_and_multiple_choice_limit(self):
        single = await self._create(allow_change=False)
        await self.service.vote(single["public_token"], [1], "browser-a")
        with self.assertRaises(PollError):
            await self.service.vote(single["public_token"], [2], "browser-a")

        multiple = await self._create(multiple_choice=True, max_choices=2)
        with self.assertRaises(PollError):
            await self.service.vote(multiple["public_token"], [1, 2, 3], "browser-b")
        await self.service.vote(multiple["public_token"], [1, 3], "browser-b")

    async def test_deadline_closes_poll_and_http_vote_cannot_lag_scheduler(self):
        now = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        poll = await self._create(
            deadline="2026-09-17 19:00",
            now=now,
        )
        with self.assertRaises(PollClosedError):
            await self.service.vote(
                poll["public_token"], [1], "browser-a", now=now + timedelta(hours=9),
            )
        self.assertEqual(self.storage.get_poll(poll["id"])["status"], "CLOSED")

        second = await self._create(deadline="2026-09-17 19:00", now=now)
        closed = await self.service.close_due_polls(now + timedelta(hours=9))
        self.assertEqual({item["id"] for item in closed}, {second["id"]})
        self.assertEqual(self.storage.get_poll(second["id"])["status"], "CLOSED")
        self.assertEqual(await self.service.close_due_polls(now + timedelta(hours=10)), [])

    async def test_after_close_visibility_hides_then_reveals_results(self):
        poll = await self._create(result_visibility="after_close")
        await self.service.vote(poll["public_token"], [1], "browser-a")
        hidden = await self.service.get_public_result(poll["public_token"])
        self.assertFalse(hidden["visible"])
        await self.service.close_poll(poll["id"], "manual")
        shown = await self.service.get_public_result(poll["public_token"])
        self.assertTrue(shown["visible"])
        self.assertEqual(shown["result"]["participant_count"], 1)

    async def test_storage_migration_creates_poll_tables(self):
        tables = {
            row[0] for row in self.storage._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            ).fetchall()
        }
        self.assertTrue({"polls", "poll_options", "poll_ballots"} <= tables)

    async def test_poll_control_uses_operator_and_group_admin_authorization(self):
        main_module = _load_main_module()

        class Event:
            def __init__(self, sender, private, role="member"):
                self.sender = sender
                self.private = private
                self.role = role

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

    async def test_web_page_vote_cookie_result_and_xss_escape(self):
        self.assertIsNotNone(PollWeb, "PollWeb is not implemented")
        poll = await self._create(
            title="<script>alert(1)</script>",
            description="<img src=x onerror=alert(1)>",
            options=["安全选项", "<b>危险</b>"],
            result_visibility="live",
        )
        from aiohttp.test_utils import TestClient, TestServer

        web_app = PollWeb(self.service).create_app()
        async with TestClient(TestServer(web_app)) as client:
            page = await client.get(f"/poll/{poll['public_token']}")
            self.assertEqual(page.status, 200)
            body = await page.text()
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
            self.assertNotIn("<script>alert(1)</script>", body)
            self.assertIn("lumielle_poll_voter", page.headers.get("Set-Cookie", ""))

            voted = await client.post(
                f"/api/poll/{poll['public_token']}/vote",
                json={"choices": [2]},
            )
            self.assertEqual(voted.status, 200)
            result = await (await client.get(f"/api/poll/{poll['public_token']}/result")).json()
            self.assertTrue(result["visible"])
            self.assertEqual(result["result"]["participant_count"], 1)

    async def test_healthz_returns_ok(self):
        self.assertIsNotNone(PollWeb, "PollWeb is not implemented")
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(PollWeb(self.service).create_app())) as client:
            response = await client.get("/healthz")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"ok": True})

    async def test_auto_publish_result_marks_once(self):
        main_module = _load_main_module()
        poll = await self._create()
        await self.service.vote(poll["public_token"], [1], "browser-a")
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

    async def test_web_deadline_close_is_recovered_and_published_once(self):
        main_module = _load_main_module()
        now = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        poll = await self._create(deadline="2026-09-17 19:00", now=now)
        with self.assertRaises(PollClosedError):
            await self.service.vote(
                poll["public_token"], [1], "browser-a", now=now + timedelta(hours=9),
            )
        self.assertEqual(self.storage.get_poll(poll["id"])["status"], "CLOSED")

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
        self.assertEqual(self.storage.get_poll(poll["id"])["last_error"], "announcement failed")

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

    async def test_web_deadline_uses_configured_timezone(self):
        poll = await self._create(deadline="2026-09-18 22:00")
        page = PollWeb(self.service)._render_page(
            poll, [], {"visible": False, "message": "结果将在投票结束后公布。"},
        )
        self.assertIn("2026-09-18 22:00", page)
        self.assertNotIn("2026-09-18T14:00:00+00:00", page)

    async def test_web_generic_500_hides_internal_exception(self):
        class FailingService:
            public_base_url = "https://poll.example.com"

            async def vote(self, *_args, **_kwargs):
                raise RuntimeError("/secret/internal/path")

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(PollWeb(FailingService()).create_app())) as client:
            response = await client.post("/api/poll/unknown/vote", json={"choices": [1]})
            payload = await response.json()
        self.assertEqual(response.status, 500)
        self.assertEqual(payload["error"], "投票服务暂时不可用，请稍后重试。")
        self.assertNotIn("/secret/internal/path", payload["error"])
        self.assertNotIn("RuntimeError", payload["error"])


if __name__ == "__main__":
    unittest.main()
