import sqlite3
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import Storage, TaskManager


class GroupHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(self.storage, timezone_name="Asia/Shanghai")
        await self.manager.bind_group("班群", "123456789", "qq-main", "operator")
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    async def test_bound_group_history_is_enabled_by_default(self):
        saved = await self.manager.archive_group_message(
            "qq-main", "123456789", "1001", "张三", "今天下午开会",
            sent_at=self.now, source_message_id="live-1", message_seq=11,
        )
        status = await self.manager.archive_status("班群", "qq-main")
        self.assertIsNotNone(saved)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["setting_source"], "default")
        self.assertEqual(status["count"], 1)

    async def test_explicit_off_survives_reopen_and_remains_an_override(self):
        await self.manager.set_archive("班群", False, "qq-main")
        self.storage.close()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(
            self.storage, timezone_name="Asia/Shanghai", history_default_enabled=True,
        )
        await self.manager.archive_group_message(
            "qq-main", "123456789", "1001", "张三", "不应归档",
            sent_at=self.now, source_message_id="off-1",
        )
        status = await self.manager.archive_status("班群", "qq-main")
        self.assertFalse(status["enabled"])
        self.assertEqual(status["setting_source"], "override")
        self.assertEqual(status["count"], 0)

    async def test_old_database_migration_preserves_data_and_defaults_history_on(self):
        old_dir = Path(self.temp_dir.name) / "old-database"
        old_dir.mkdir()
        old_db = old_dir / "lumielle_nexus.db"
        connection = sqlite3.connect(old_db)
        connection.executescript("""
            CREATE TABLE group_bindings (
                alias TEXT NOT NULL, group_id TEXT NOT NULL, platform_id TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(platform_id, alias), UNIQUE(platform_id, group_id)
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
                group_id TEXT NOT NULL, group_alias TEXT NOT NULL, platform_id TEXT NOT NULL,
                creator_id TEXT NOT NULL, creator_private_origin TEXT NOT NULL,
                created_at TEXT NOT NULL, run_at TEXT, payload TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, updated_at TEXT NOT NULL, finished_at TEXT,
                parent_id TEXT, occurrence_key TEXT
            );
            CREATE TABLE collection_entries (
                task_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                raw_message TEXT NOT NULL, parsed_data TEXT NOT NULL,
                submitted_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id, sender_id)
            );
            CREATE TABLE workflow_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                source_message_id TEXT, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                message_text TEXT NOT NULL, sent_at TEXT NOT NULL, captured_at TEXT NOT NULL,
                deterministic_handled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE group_archive_settings (
                platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0, enabled_at TEXT,
                updated_at TEXT NOT NULL, PRIMARY KEY(platform_id, group_id)
            );
            CREATE TABLE group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform_id TEXT NOT NULL,
                group_id TEXT NOT NULL, source_message_id TEXT, sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL, message_text TEXT NOT NULL,
                sent_at TEXT NOT NULL, archived_at TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT INTO group_bindings VALUES (?, ?, ?, ?, ?)",
            ("班群", "123456789", "qq-main", "operator", self.now.isoformat()),
        )
        connection.execute(
            "INSERT INTO tasks (id,type,status,group_id,group_alias,platform_id,creator_id,"
            "creator_private_origin,created_at,payload,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("C-20261006-001", "COLLECTION", "ACTIVE", "123456789", "班群",
             "qq-main", "operator", "private:unrelated", self.now.isoformat(),
             '{"fields":["返校状态"]}', self.now.isoformat()),
        )
        connection.execute(
            "INSERT INTO collection_entries VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("C-20261006-001", "1001", "张三", "返校状态：已返校",
             '{"返校状态":"已返校"}', self.now.isoformat(), self.now.isoformat()),
        )
        connection.execute(
            "INSERT INTO workflow_messages (task_id,source_message_id,sender_id,sender_name,"
            "message_text,sent_at,captured_at,deterministic_handled) VALUES (?,?,?,?,?,?,?,?)",
            ("C-20261006-001", "workflow-1", "1001", "张三", "已返校",
             self.now.isoformat(), self.now.isoformat(), 1),
        )
        connection.execute(
            "INSERT INTO group_messages (platform_id,group_id,source_message_id,sender_id,"
            "sender_name,message_text,sent_at,archived_at) VALUES (?,?,?,?,?,?,?,?)",
            ("qq-main", "123456789", "history-1", "1001", "张三", "已返校",
             self.now.isoformat(), self.now.isoformat()),
        )
        connection.commit()
        connection.close()

        migrated = Storage(old_dir)
        manager = TaskManager(migrated, timezone_name="Asia/Shanghai")
        try:
            status = await manager.archive_status("班群", "qq-main")
            self.assertTrue(status["enabled"])
            self.assertEqual(status["setting_source"], "default")
            self.assertEqual(migrated.get_task("C-20261006-001")["status"], "ACTIVE")
            self.assertEqual(len(migrated.list_entries("C-20261006-001")), 1)
            workflow = migrated.list_workflow_messages("C-20261006-001")
            self.assertEqual(len(workflow), 1)
            self.assertEqual(workflow[0]["analysis_state"], "deterministic")
            history = migrated.search_group_messages(
                "qq-main", "123456789", "", None, None, 10,
            )
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["source"], "live")
            columns = {
                row[1] for row in migrated._conn.execute(
                    "PRAGMA table_info(workflow_messages)",
                ).fetchall()
            }
            self.assertIn("source_message_seq", columns)
            self.assertIn("analysis_state", columns)
            self.assertIn("no_data_recheck_attempts", columns)
            tables = {
                row[0] for row in migrated._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall()
            }
            self.assertIn("group_history_sync_state", tables)
        finally:
            migrated.close()

    async def test_bounded_backfill_persists_and_deduplicates_by_message_identity(self):
        pages = {
            0: [
                self._message(30, "2026-10-06T12:00:00+00:00", "新消息"),
                self._message(20, "2026-10-06T11:00:00+00:00", "中间消息"),
            ],
            19: [
                self._message(10, "2026-10-06T10:00:00+00:00", "最早消息"),
            ],
        }
        calls = []

        async def fetch(group_id, message_seq=0, reverse_order=True):
            calls.append((group_id, message_seq, reverse_order))
            return pages.get(message_seq, [])

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", "2026-10-06 18:00", "2026-10-06 19:30",
            fetch, now=self.now,
        )
        self.assertEqual(result["coverage_status"], "FULL")
        self.assertEqual(result["fetched_count"], 3)
        self.assertEqual(result["inserted_count"], 3)
        self.assertEqual(calls[0][1], 0)
        self.assertEqual(calls[1][1], 19)
        rows = await self.manager.search_messages(
            "班群", start_time="2026-10-06 18:00", end_time="2026-10-06 19:30",
            platform_id="qq-main",
        )
        self.assertEqual({row["message_text"] for row in rows}, {"最早消息", "中间消息"})
        repeated = await self.manager.ensure_group_history(
            "班群", "qq-main", "2026-10-06 18:00", "2026-10-06 19:30",
            fetch, now=self.now,
        )
        self.assertEqual(repeated["fetched_count"], 0)
        self.assertEqual(self.storage.count_group_messages("qq-main", "123456789"), 3)
        self.storage.close()
        self.storage = Storage(Path(self.temp_dir.name))
        self.manager = TaskManager(self.storage, timezone_name="Asia/Shanghai")
        self.assertEqual(self.storage.count_group_messages("qq-main", "123456789"), 3)

    async def test_live_and_backfill_same_message_identity_is_stored_once(self):
        timestamp = self.now - timedelta(minutes=10)
        await self.manager.archive_group_message(
            "qq-main", "123456789", "1001", "张三", "同一条回复",
            sent_at=timestamp, source_message_id="same-message", message_seq=77,
        )

        async def fetch(_group_id, message_seq=0):
            self.assertEqual(message_seq, 0)
            return [self._message(77, timestamp.isoformat(), "同一条回复") | {
                "message_id": "same-message",
            }]

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", (timestamp - timedelta(minutes=1)).isoformat(),
            (timestamp + timedelta(minutes=1)).isoformat(), fetch, now=self.now,
        )
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["inserted_count"], 0)
        self.assertEqual(self.storage.count_group_messages("qq-main", "123456789"), 1)
        rows = await self.manager.search_messages(
            "班群", start_time=(timestamp - timedelta(minutes=1)).isoformat(),
            end_time=(timestamp + timedelta(minutes=1)).isoformat(), platform_id="qq-main",
        )
        self.assertEqual(rows[0]["source"], "live")

    async def test_stuck_history_cursor_is_partial(self):
        page = [self._message(100, "2026-10-06T12:00:00+00:00", "较新")]

        async def fetch(_group_id, message_seq=0, reverse_order=True):
            return page

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", "2026-10-05 20:00", "2026-10-06 20:00",
            fetch, max_pages=4, now=self.now,
        )
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "cursor_stuck")
        self.assertFalse(result["history_exhausted"])

    async def test_partial_sync_bounds_are_not_reused_as_full_coverage(self):
        self.storage.update_group_history_sync_state(
            "qq-main", "123456789",
            covered_from=(self.now - timedelta(days=3)).isoformat(),
            covered_to=self.now.isoformat(),
            oldest_seq=10,
            history_exhausted=False,
            coverage_status="PARTIAL",
            last_sync_at=self.now.isoformat(),
            last_sync_error="cursor_stuck",
        )
        calls = []

        async def broken_fetch(_group_id, message_seq=0):
            calls.append(message_seq)
            raise RuntimeError("history still unavailable")

        result = await self.manager.ensure_group_history(
            "班群", "qq-main",
            (self.now - timedelta(hours=2)).isoformat(),
            (self.now - timedelta(hours=1)).isoformat(),
            broken_fetch, now=self.now,
        )
        self.assertEqual(calls, [0])
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "api_error")

    async def test_max_page_budget_is_partial_not_full(self):
        async def fetch(_group_id, message_seq=0):
            return [self._message(100, self.now.isoformat(), "较新消息")]

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", "2026-10-05 20:00", "2026-10-06 20:00",
            fetch, max_pages=1, now=self.now,
        )
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "max_pages")

    async def test_history_api_failure_keeps_local_search_and_never_claims_full(self):
        await self.manager.archive_group_message(
            "qq-main", "123456789", "1001", "张三", "本地实时消息",
            sent_at=self.now, source_message_id="local-1",
        )

        async def broken_fetch(*_args, **_kwargs):
            raise RuntimeError("offline")

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", "2026-10-06 18:00", "2026-10-06 20:00",
            broken_fetch, now=self.now,
        )
        local = await self.manager.search_messages(
            "班群", start_time="2026-10-06 18:00", end_time="2026-10-06 20:00",
            platform_id="qq-main",
        )
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertIn("offline", result["last_error"])
        self.assertEqual([row["message_text"] for row in local], ["本地实时消息"])

    async def test_retention_prune_invalidates_old_coverage_for_reconciliation(self):
        old_time = self.now - timedelta(days=11)
        message = self._message(10, old_time.isoformat(), "保留期外的历史")
        calls = []

        async def fetch(_group_id, message_seq=0):
            calls.append(message_seq)
            return [message] if message_seq == 0 else []

        start = self.now - timedelta(days=10)
        end = self.now - timedelta(days=8)
        first = await self.manager.ensure_group_history(
            "班群", "qq-main", start.isoformat(), end.isoformat(), fetch, now=self.now,
        )
        self.assertEqual(first["coverage_status"], "FULL")
        removed = await self.manager.prune_archive(now=self.now, retention_days=7)
        self.assertEqual(removed, 1)
        status = await self.manager.archive_status("班群", "qq-main")
        self.assertEqual(status["coverage_status"], "PARTIAL")
        second = await self.manager.ensure_group_history(
            "班群", "qq-main", start.isoformat(), end.isoformat(), fetch, now=self.now,
        )
        self.assertGreater(second["fetched_count"], 0)
        self.assertGreaterEqual(len(calls), 2)

    async def test_clear_archive_invalidates_sync_coverage_without_clearing_override(self):
        message = self._message(20, (self.now - timedelta(hours=1)).isoformat(), "清空前")

        async def fetch(_group_id, message_seq=0):
            return [message] if message_seq == 0 else []

        start = self.now - timedelta(hours=2)
        end = self.now
        await self.manager.ensure_group_history(
            "班群", "qq-main", start.isoformat(), end.isoformat(), fetch, now=self.now,
        )
        await self.manager.clear_archive("班群", "qq-main")
        status = await self.manager.archive_status("班群", "qq-main")
        self.assertEqual(status["coverage_status"], "UNKNOWN")
        self.assertTrue(status["enabled"])
        again = await self.manager.ensure_group_history(
            "班群", "qq-main", start.isoformat(), end.isoformat(), fetch, now=self.now,
        )
        self.assertGreater(again["fetched_count"], 0)

    async def test_empty_latest_history_proves_full_empty_window(self):
        async def fetch(_group_id, message_seq=0, reverse_order=True):
            self.assertEqual(message_seq, 0)
            return []

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", "2026-10-06 18:00", "2026-10-06 19:30",
            fetch, now=self.now,
        )
        self.assertEqual(result["coverage_status"], "FULL")
        self.assertEqual(result["stop_reason"], "history_exhausted")
        self.assertTrue(result["history_exhausted"])

    async def test_nonempty_unparseable_history_page_never_proves_exhaustion(self):
        start = self.now - timedelta(hours=2)
        end = self.now
        self.storage.update_group_history_sync_state(
            "qq-main", "123456789",
            covered_from=(self.now - timedelta(hours=3)).isoformat(),
            covered_to=(self.now - timedelta(hours=1)).isoformat(),
            oldest_seq=40,
            history_exhausted=False,
            coverage_status="PARTIAL",
            last_sync_at=(self.now - timedelta(minutes=30)).isoformat(),
            last_sync_error="prior bounded sync",
        )

        async def fetch(_group_id, message_seq=0):
            return [{
                "message_seq": 90,
                "time": self.now.timestamp(),
                "sender": {"user_id": "1001"},
                "raw_message": "",
                "message": [{"type": "image", "data": {"url": "ignored"}}],
            }]

        result = await self.manager.ensure_group_history(
            "班群", "qq-main", start.isoformat(), end.isoformat(), fetch,
            now=self.now,
        )
        self.assertEqual(result["coverage_status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "unparseable_page")
        self.assertFalse(result["history_exhausted"])

    async def test_private_operator_can_query_multiple_bound_groups_across_sessions(self):
        from test_poll import _load_main_module

        await self.manager.bind_group("测试群", "987654321", "qq-main", "operator")
        await self.manager.archive_group_message(
            "qq-main", "123456789", "1001", "张三", "班群里的历史",
            sent_at=self.now, source_message_id="class-msg",
        )
        await self.manager.archive_group_message(
            "qq-main", "987654321", "1002", "李四", "测试群里的历史",
            sent_at=self.now, source_message_id="test-msg",
        )

        class Event:
            unified_msg_origin = "private:unrelated-session"

            def is_private_chat(self):
                return True

            def is_admin(self):
                return False

            def get_sender_id(self):
                return "operator"

            def get_platform_name(self):
                return "aiocqhttp"

            def get_platform_id(self):
                return "qq-main"

        plugin = object.__new__(_load_main_module().LumielleNexus)
        plugin.operator_ids = {"operator"}
        plugin.manager = self.manager
        first = await plugin._search_group_history(
            Event(), "班群", keyword="班群里的历史", ensure_synced=False,
        )
        second = await plugin._search_group_history(
            Event(), "987654321", keyword="测试群里的历史", ensure_synced=False,
        )
        self.assertIn("班群里的历史", first)
        self.assertIn("测试群里的历史", second)
        self.assertIn("不可信原文", first)

    async def test_private_non_operator_cannot_query_bound_history(self):
        from test_poll import _load_main_module

        class Event:
            unified_msg_origin = "private:session"

            def is_private_chat(self):
                return True

            def is_admin(self):
                return False

            def get_sender_id(self):
                return "ordinary-user"

            def get_platform_name(self):
                return "aiocqhttp"

            def get_platform_id(self):
                return "qq-main"

        plugin = object.__new__(_load_main_module().LumielleNexus)
        plugin.operator_ids = {"operator"}
        plugin.manager = self.manager
        result = await plugin._search_group_history(Event(), "班群", ensure_synced=False)
        self.assertIn("operator 权限", result)

    async def test_history_search_reports_exact_local_pagination(self):
        from test_poll import _load_main_module

        for index, text in enumerate(("较早消息", "中间消息", "最新消息"), 1):
            await self.manager.archive_group_message(
                "qq-main", "123456789", str(1000 + index), f"成员{index}", text,
                sent_at=self.now + timedelta(seconds=index), source_message_id=f"page-{index}",
            )

        class Event:
            unified_msg_origin = "private:any-session"

            def is_private_chat(self):
                return True

            def is_admin(self):
                return False

            def get_sender_id(self):
                return "operator"

            def get_platform_name(self):
                return "aiocqhttp"

            def get_platform_id(self):
                return "qq-main"

        plugin = object.__new__(_load_main_module().LumielleNexus)
        plugin.operator_ids = {"operator"}
        plugin.manager = self.manager
        first = await plugin._search_group_history(
            Event(), "班群", limit=1, ensure_synced=False,
        )
        self.assertIn("本地还有更多：是", first)
        self.assertIn("下一页 before_id：", first)
        before_id = int(first.split("下一页 before_id：", 1)[1].splitlines()[0])
        second = await plugin._search_group_history(
            Event(), "班群", limit=2, before_id=before_id, ensure_synced=False,
        )
        self.assertIn("本地还有更多：否", second)
        self.assertIn("较早消息", second)

    async def test_group_admin_is_scoped_to_current_group_and_member_is_denied(self):
        from test_poll import _load_main_module

        await self.manager.bind_group("测试群", "987654321", "qq-main", "operator")

        class Event:
            unified_msg_origin = "group:session"

            def __init__(self, role):
                self.role = role

            def is_private_chat(self):
                return False

            def get_platform_name(self):
                return "aiocqhttp"

            def get_platform_id(self):
                return "qq-main"

            def get_group_id(self):
                return "123456789"

            def get_sender_id(self):
                return "1001"

        plugin = object.__new__(_load_main_module().LumielleNexus)
        plugin.manager = self.manager
        plugin.operator_ids = set()
        plugin._adapter = lambda event: types.SimpleNamespace(
            get_group_member_info=lambda *_args: _resolved({"role": event.role}),
        )
        admin_result = await plugin._search_group_history(
            Event("admin"), "班群", ensure_synced=False,
        )
        cross_group_result = await plugin._search_group_history(
            Event("admin"), "测试群", ensure_synced=False,
        )
        member_result = await plugin._search_group_history(
            Event("member"), "班群", ensure_synced=False,
        )
        self.assertIn("班群", admin_result)
        self.assertIn("不能跨群查询", cross_group_result)
        self.assertIn("不是当前群的 QQ 群主/管理员", member_result)

    async def test_explicit_history_off_blocks_local_only_search(self):
        from test_poll import _load_main_module

        await self.manager.archive_group_message(
            "qq-main", "123456789", "1001", "张三", "既有私密历史",
            sent_at=self.now, source_message_id="private-history",
        )
        await self.manager.set_archive("班群", False, "qq-main")

        class Event:
            unified_msg_origin = "private:session"

            def is_private_chat(self):
                return True

            def is_admin(self):
                return False

            def get_sender_id(self):
                return "operator"

            def get_platform_name(self):
                return "aiocqhttp"

            def get_platform_id(self):
                return "qq-main"

        plugin = object.__new__(_load_main_module().LumielleNexus)
        plugin.operator_ids = {"operator"}
        plugin.manager = self.manager
        result = await plugin._search_group_history(
            Event(), "班群", ensure_synced=False,
        )
        self.assertIn("显式关闭", result)
        self.assertNotIn("既有私密历史", result)
        self.assertIn("显式关闭", await plugin._search_messages(Event(), "班群"))
        plugin.context = types.SimpleNamespace(
            get_current_chat_provider_id=lambda *_args: self.fail("provider must not be queried"),
        )
        summary = await plugin._summarize_group(Event(), "班群")
        self.assertIn("显式关闭", summary)

    @staticmethod
    def _message(seq, sent_at, text):
        return {
            "message_id": f"m-{seq}",
            "message_seq": seq,
            "time": datetime.fromisoformat(sent_at).timestamp(),
            "sender": {"user_id": "1001", "nickname": "张三", "card": ""},
            "raw_message": text,
        }


async def _resolved(value):
    return value


if __name__ == "__main__":
    unittest.main()
