# Lumielle Nexus Phase 2 Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Lumielle Nexus task system with durable weekly schedules, DDL and course reminders, collection chase operations, and retry backoff while preserving the single SQLite-backed scheduler.

**Architecture:** Keep `TaskManager + Storage + QQAdapter + scheduler` as the only runtime path. DDL, RECURRING, and COURSE are logical parent tasks; each actual send is a `REMINDER` child identified by `(parent_id, occurrence_key)`. Collection chase uses the same durable reminder queue with a `payload.kind` dispatch, and all scheduling times remain UTC in SQLite while user output uses the configured timezone.

**Tech Stack:** Python stdlib (`asyncio`, `sqlite3`, `datetime`, `zoneinfo`, `json`), `openpyxl`, AstrBot 4.28 public plugin API, aiocqhttp/OneBot v11.

**Spec:** User-provided Phase 2 scheduling specification in the task conversation.

## Global Constraints

- Target AstrBot version is `>=4.28.0,<5`; do not modify AstrBot Core or use deprecated registration APIs.
- GitHub `main` is source of truth; preserve existing `main.py / core.py / storage.py / qq_adapter.py / exporter.py` structure.
- Use one scheduler loop and SQLite; do not add APScheduler, Redis, Celery, ORM, WebUI, cron/RRULE, archive, moderation, or free-text collection extraction.
- Runtime database, exports, AstrBot config, cache, credentials, and tokens must not enter Git.
- All control tools and commands require private chat plus AstrBot admin or configured operator authorization.
- Internal timestamps are UTC ISO strings; recurring, DDL, course, and user-facing output use the configured timezone.

---

### Task 1: Add backward-compatible task schema and task-list boundaries

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Test: `tests/test_core.py`

**Interfaces:**
- `Storage` adds nullable `parent_id` and `occurrence_key` columns to existing `tasks`, an index on `parent_id`, and a unique partial index on `(parent_id, occurrence_key)` where both are non-null.
- `Storage.create_task` accepts optional `parent_id` and `occurrence_key`.
- `Storage.list_tasks(..., include_children=False)` hides child reminders by default.
- `TaskManager.list_tasks(..., include_children=False)` forwards that boundary.
- `Storage.cancel_task` and a new cascade helper cancel only pending children without deleting history.

- [ ] **Step 1: Write the failing migration and listing tests.** Create an old-schema SQLite database with a binding, standalone reminder, collection, and collection entry; reopen with `Storage` and assert all rows survive, new columns exist, and duplicate non-null occurrences are rejected while null occurrences remain allowed.
- [ ] **Step 2: Run the migration tests and confirm they fail because the columns/index/API are absent.**
- [ ] **Step 3: Implement additive migration using `PRAGMA table_info`, `ALTER TABLE ADD COLUMN`, and idempotent `CREATE INDEX IF NOT EXISTS`; never rebuild or delete `tasks`.
- [ ] **Step 4: Add parent/occurrence parameters and hide `parent_id IS NOT NULL` children from default task lists; implement pending-child cascade cancellation inside one SQLite transaction.
- [ ] **Step 5: Run the focused migration/listing tests and confirm the original 19 tests remain green.

### Task 2: Implement deterministic weekly recurrence primitives

**Files:**
- Modify: `core.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Add `next_weekly_occurrence(after, weekdays, time_of_day, timezone_name, start_date=None, end_date=None) -> datetime | None`, returning an aware UTC datetime.
- Add strict validators for weekday values `1..7`, `HH:MM`, optional ISO dates, and configured timezone.
- Add helpers to format local schedules and calculate a missed-occurrence grace decision without changing stored UTC formats.

- [ ] **Step 1: Write failing tests for Monday/Wednesday schedules, timezone conversion, start/end dates, cross-week rollover, and a missed occurrence older than 120 seconds.
- [ ] **Step 2: Run those tests and confirm the recurrence functions are missing or reject the required cases.
- [ ] **Step 3: Implement the pure calculation by converting `after` to the configured zone, checking at most the next seven local dates, applying inclusive date bounds, and returning UTC.
- [ ] **Step 4: Implement the 120-second grace policy as a small pure decision/helper used by materialization; old occurrences advance directly to the next future occurrence.
- [ ] **Step 5: Run recurrence tests and verify no scheduler or database code is needed by the pure functions.

### Task 3: Add weekly RECURRING and COURSE parent tasks plus materialization

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Modify: `main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Add `TaskManager.create_recurring_reminder(group, weekdays, time_of_day, message, mention_all, start_date, end_date, platform_id, creator_id, origin) -> task` with `S-` IDs.
- Add `TaskManager.create_course(group, course_name, weekdays, start_time, location, remind_before_minutes, start_date, end_date, mention_all, platform_id, creator_id, origin) -> task` with `K-` IDs.
- Add `TaskManager.materialize_due_schedules(now=None) -> list[dict]` that creates at most one next `REMINDER` child per active parent and advances `parent.run_at` transactionally.
- Parent payloads store the complete validated rule; child payloads store the final message, mention flag, parent type, and occurrence metadata.

- [ ] **Step 1: Write failing tests for recurring creation, course reminder time (`start_time - offset`), midnight-crossing course times, end-date completion, and duplicate materialization after a repeated call.
- [ ] **Step 2: Run the new tests and confirm parent creation/materialization is not implemented.
- [ ] **Step 3: Implement parent creation with `S-`/`K-` IDs, UTC `run_at` equal to the next reminder occurrence, and exact validation including `remind_before_minutes >= 0` and a course offset that does not produce an invalid schedule.
- [ ] **Step 4: Implement materialization with the unique occurrence key, one child per active parent, parent advancement, missed-occurrence skip, and automatic `COMPLETED` after `end_date`.
- [ ] **Step 5: Register `nexus_create_recurring_reminder` and `nexus_create_course` with explicit weekday/timezone/offset descriptions; return local next-run information.
- [ ] **Step 6: Run focused schedule tests and confirm default task listing shows parents but not child reminders.

### Task 4: Add DDL parents, offsets, deadline completion, and cancellation cascade

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Modify: `main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Add `TaskManager.create_ddl(group, title, deadline, remind_before_minutes, message, mention_all, platform_id, creator_id, origin) -> task` with `D-` IDs.
- DDL payload stores title, deadline, normalized offsets, message, and mention flag; parent `run_at` is the deadline.
- Add maintenance that materializes only future offset children and marks an active parent completed after the deadline.

- [ ] **Step 1: Write failing tests for future offsets, duplicate/unsorted offsets, skipped past offsets, offset `0`, oversized/negative offsets, past deadline rejection, and parent cancellation cascading pending children.
- [ ] **Step 2: Run the DDL tests and confirm the creation and lifecycle APIs are absent.
- [ ] **Step 3: Implement validation with a maximum of 20 offsets and `366 * 24 * 60` minutes; report skipped offsets in the creation result without creating stale children.
- [ ] **Step 4: Implement DDL materialization and deadline completion while preserving completed child history; ensure offset `0` is created only when explicitly requested.
- [ ] **Step 5: Extend `cancel_task` to allow only pending standalone reminders or active DDL/RECURRING/COURSE parents, then cascade pending children; retain Collection stop-only semantics.
- [ ] **Step 6: Register `nexus_create_ddl`, update cancellation/help text, and run focused DDL tests plus the existing cancellation regressions.

### Task 5: Harden reminder retry and standalone past-time validation

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Modify: `main.py`
- Modify: `README.md`
- Test: `tests/test_core.py`

**Interfaces:**
- `TaskManager.create_reminder` rejects times more than 60 seconds in the past after timezone normalization.
- `TaskManager.finish_reminder` schedules retry `run_at` at 30, 120, 300 seconds, then capped exponential backoff up to 900 seconds; final failures remain `FAILED`.
- Scheduler maintenance invokes schedule materialization before claiming due reminders.

- [ ] **Step 1: Write failing tests for past standalone reminders, retry run-at future movement, the first three backoff values, cap behavior, and final failure.
- [ ] **Step 2: Run the retry tests and confirm old immediate-retry behavior fails the new assertions.
- [ ] **Step 3: Implement the 60-second grace check and DB-backed retry schedule; leave successful completion and crash recovery semantics intact.
- [ ] **Step 4: Update scheduler order to run parent maintenance before `due_tasks`, and route every claimed child through one reminder executor.
- [ ] **Step 5: Update README with weekly-only recurrence and existing at-least-once limitations; run retry and reminder regression tests.

### Task 6: Implement collection missing-member chase and batched @mentions

**Files:**
- Modify: `qq_adapter.py`
- Modify: `core.py`
- Modify: `main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Add `QQAdapter.send_group_at_members(group_id, user_ids, text) -> list[Any]`, sending batches of at most 20 OneBot `at` segments plus text.
- Add `TaskManager.schedule_collection_chase(task_id, run_at, message, repeat_interval_minutes, platform_id, creator_id, origin) -> task` using a child `REMINDER` payload with `kind="collection_chase"`.
- Scheduler dispatches normal reminders and chase reminders through the same claim/retry path.

- [ ] **Step 1: Write failing tests for missing human members, bot/robot exclusion, zero-missing no-send, completed collection skip, repeat interval `< 60` rejection, 20-member batching, and repeat termination.
- [ ] **Step 2: Run chase tests and confirm the adapter/core methods and scheduler dispatch are missing.
- [ ] **Step 3: Implement batched `send_group_at_members` in `qq_adapter.py`; catch protocol errors through the existing adapter error type.
- [ ] **Step 4: Implement chase task validation for active collections and repeat intervals from 60 minutes through 7 days; use shared member stats to build current missing IDs at execution time.
- [ ] **Step 5: Implement scheduler chase behavior: no message for zero missing, no @all, at most 20 IDs per message, no action for non-active collections, and next chase only while missing members remain.
- [ ] **Step 6: Register `nexus_schedule_collection_chase` and run focused chase/adapter tests with all existing collection tests.

### Task 7: Add task details, task display, documentation, and versioning

**Files:**
- Modify: `main.py`
- Modify: `metadata.yaml`
- Modify: `README.md`
- Modify: `tests/test_core.py`

**Interfaces:**
- Add `nexus_get_task(task_id)` and `/nexus task <task-id>` with type-specific human-readable local-time details and no raw JSON/absolute paths.
- Update `_task_line` and list output for DDL deadline, recurring next occurrence, course next reminder, collection title, and standalone reminder time.
- Version becomes `0.2.0` in metadata and user-facing documentation.

- [ ] **Step 1: Write failing contract tests for all five new tools, `0.2.0`, local task-list/detail output, and absence of child reminders in default lists.
- [ ] **Step 2: Run the contract tests and confirm the new registrations/version/output are absent.
- [ ] **Step 3: Implement details and display formatting using the configured timezone and payload fields; keep authorization checks in every tool/command.
- [ ] **Step 4: Update README current capabilities, command fallback, restart persistence, weekly-only recurrence limits, DDL/course/chase examples, retry backoff, and explicit out-of-scope list.
- [ ] **Step 5: Run all tests and confirm existing 19 tests plus new phase-2 tests pass.

### Task 8: Final compatibility verification and delivery

**Files:**
- Verify: all repository files and Git state

- [ ] **Step 1: Run `python3 -m compileall .`.
- [ ] **Step 2: Run `python3 -m unittest discover -s tests -v` and record the exact pass count.
- [ ] **Step 3: Run `git diff --check`; run `ruff check .` only if Ruff is installed.
- [ ] **Step 4: Symlink the plugin into an official AstrBot v4.28.0 `data/plugins` tree and import `data.plugins.astrbot_plugin_lumielle_nexus.main`; verify the Star class and new LLM tools register.
- [ ] **Step 5:** Confirm no DB, exports, cache, local config, token, or secret is staged; commit with `feat: add scheduling and class task workflows` and push `main` without force.
