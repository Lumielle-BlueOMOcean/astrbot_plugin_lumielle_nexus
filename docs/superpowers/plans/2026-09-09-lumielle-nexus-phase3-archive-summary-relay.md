# Lumielle Nexus Phase 3 Archive, Summary, and Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in group text archiving, bounded archive search and summaries, durable weekly summaries, and explicit-confirmation cross-group relay without replacing the existing task scheduler.

**Architecture:** Keep `TaskManager + Storage + QQAdapter + scheduler` as the only runtime path. Store archive settings/messages in the existing SQLite database; represent weekly summaries as `SUMMARY` parents with `REMINDER` children; represent relay as a pending `RELAY` task that is sent only after a separate confirmation tool call.

**Tech Stack:** Python stdlib (`asyncio`, `sqlite3`, `json`, `datetime`, `zoneinfo`, `pathlib`), `openpyxl`, AstrBot 4.28 public plugin API, and aiocqhttp/OneBot v11.

**Spec:** User-provided Phase 3 archive, summary, weekly summary, and relay specification in the task conversation.

## Global Constraints

- Target AstrBot compatibility remains `>=4.28.0,<5`; do not modify AstrBot Core or use deprecated APIs.
- Archive is opt-in per bound group, text-only, and never records private chats.
- Runtime data remains under `data/plugin_data/astrbot_plugin_lumielle_nexus/`; no database, archive rows, exports, credentials, or tokens enter Git.
- Use the existing scheduler and SQLite; do not add RAG, embeddings, vector databases, Redis, ORM, WebUI, HTTP services, or a second scheduler.
- All archive queries, summaries, weekly-summary controls, and relay controls require private chat plus AstrBot admin or configured operator authorization.
- Summary calls use `get_current_chat_provider_id` and `llm_generate`; summaries never create tasks automatically.
- Relay uses prepare → preview → explicit confirm → send; it has no automatic retry.

---

### Task 1: Add archive schema, settings, retention, and message CRUD

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Modify: `_conf_schema.json`
- Test: `tests/test_core.py`

**Interfaces:**
- `Storage` creates/migrates `group_archive_settings` and `group_messages` and exposes setting lookup/upsert, archive insert, status, search, and retention-prune methods.
- `TaskManager` resolves archive groups through existing bindings, clamps `archive_max_message_chars` to 256–20000, clamps retention to 0 or 7–3650 days, and keeps archive maintenance timestamps in memory only.

- [ ] **Step 1: Write failing migration and CRUD tests** for old 0.2.1 task/collection data preservation, settings defaults, duplicate source message IDs, truncation, status counts, search windows, and retention deletion.
- [ ] **Step 2: Run the focused tests and verify missing archive tables/APIs fail for the expected reasons.**
- [ ] **Step 3: Implement additive SQLite migration** with `CREATE TABLE IF NOT EXISTS`, indexes, and a partial unique source-message index; never rebuild existing tables.
- [ ] **Step 4: Implement archive setting/message methods** with parameterized SQL, UTC ISO timestamps, `LIKE` search, configured time boundaries, and per-row maximum text length.
- [ ] **Step 5: Add retention pruning** that removes only old `group_messages` rows and can be called at startup or from scheduler maintenance without raising out of the loop.
- [ ] **Step 6: Run archive storage/core tests and confirm existing scheduling/collection tests remain green.**

### Task 2: Add opt-in archive capture and archive controls

**Files:**
- Modify: `main.py`
- Modify: `core.py`
- Test: `tests/test_core.py`

**Interfaces:**
- `TaskManager.set_archive(group, enabled, platform_id)`, `archive_status(group, platform_id)`, `search_messages(...)`, and `clear_archive(group, platform_id)` all resolve bound groups.
- The single `GROUP_MESSAGE` listener archives non-self non-empty text before continuing into the existing collection parser.

- [ ] **Step 1: Write failing tests** for disabled capture, enabled capture, disable-without-delete, empty/self-message exclusion, archive plus active collection coexistence, and private-control authorization contracts.
- [ ] **Step 2: Run the focused tests and observe the missing control/capture behavior.**
- [ ] **Step 3: Implement the capture path** using `event.get_message_str()` and best-effort source message ID extraction without requiring that ID; keep collection processing unchanged and do not call `stop_event()`.
- [ ] **Step 4: Register `nexus_set_archive`, `nexus_archive_status`, `nexus_search_messages`, and `nexus_clear_archive` with explicit confirmation text for permanent deletion.**
- [ ] **Step 5: Add `/nexus archive <group> on|off` and `/nexus archive-status <group>` fallbacks, then run all archive/capture regressions.**

### Task 3: Add bounded on-demand summary generation

**Files:**
- Modify: `main.py`
- Modify: `core.py`
- Test: `tests/test_core.py`

**Interfaces:**
- `TaskManager.summary_snapshot(...)` returns deterministic full-window counts and at most 2000 chronological text rows, with a truncation note when applicable.
- A summary helper calls `context.get_current_chat_provider_id(event.unified_msg_origin)` and `context.llm_generate(chat_provider_id=..., prompt=..., system_prompt=...)` without tools or deprecated provider APIs.

- [ ] **Step 1: Write failing fake-provider tests** for short summaries, chunked summaries, deterministic metadata, provider lookup, required safety prompt phrases, and provider failure messages.
- [ ] **Step 2: Run the summary tests and confirm the provider/helper path is absent.**
- [ ] **Step 3: Implement transcript formatting** with `SUMMARY_MAX_MESSAGES = 2000`, `SUMMARY_CHUNK_CHARS = 12000`, message-boundary chunking, and a final merge call only when multiple factual-note chunks exist.
- [ ] **Step 4: Implement the system prompt** requiring source-only facts, conflict marking, confirmed-vs-discussion-vs-pending distinctions, faithful times, and no automatic task creation.
- [ ] **Step 5: Register `nexus_summarize_group`**, return full-window deterministic counts plus the model summary, and convert provider errors into readable replies while preserving archive data.
- [ ] **Step 6: Run summary-focused and full core tests.**

### Task 4: Add durable weekly summary parents and cached delivery

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Modify: `main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- `TaskManager.create_weekly_summary(...)` creates a `W-` `SUMMARY` parent containing weekday, time, lookback, focus, provider ID, and creator delivery data.
- `materialize_due_schedules()` supports `SUMMARY`, uses the existing weekly recurrence function, applies `SUMMARY_GRACE_SECONDS = 86400`, and emits a `REMINDER` child with `kind="weekly_summary"` and a logical `scheduled_run_at`.
- Summary child results cache `summary_text`, `generated_at`, `window_start`, and `window_end`; retries reuse that cache and only retry private delivery.

- [ ] **Step 1: Write failing tests** for parent creation, next occurrence, child materialization, 24-hour grace/skip, no backlog accumulation, logical scheduled windows, cancellation guards, hidden child listing, and cached retry delivery.
- [ ] **Step 2: Run weekly-summary tests and verify missing `SUMMARY` handling fails.**
- [ ] **Step 3: Extend storage/task types** without changing existing task rows or recurrence behavior; add parent cancellation support and result updates.
- [ ] **Step 4: Implement summary materialization and execution** using the saved provider ID, the logical scheduled window, fake/provider error handling, and private text chunk delivery at 3500 characters or less.
- [ ] **Step 5: Register `nexus_create_weekly_summary`**, extend task details/list formatting and `nexus_cancel_task`, then run weekly-summary and scheduling regressions.

### Task 5: Add prepare/confirm relay workflow

**Files:**
- Modify: `storage.py`
- Modify: `core.py`
- Modify: `main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- `TaskManager.prepare_relay(...)` creates an `X-` `RELAY` task in `PENDING` without sending; content is limited to 6000 characters.
- `TaskManager.confirm_relay(task_id, platform_id)` atomically claims only a pending relay and returns its payload for one-time sending; success/failure updates the task without automatic retry.

- [ ] **Step 1: Write failing tests** for prepare-without-send, plain/@all previews, confirmation, success/failure terminal states, no retry, pending cancellation, repeat-confirm rejection, platform isolation, and 6000-character validation.
- [ ] **Step 2: Run relay tests and verify missing task workflow/API failures.**
- [ ] **Step 3: Implement relay persistence and state transitions** with existing bindings and explicit `PENDING → PROCESSING → COMPLETED/FAILED` semantics.
- [ ] **Step 4: Register `nexus_prepare_relay` and `nexus_confirm_relay`** with docstrings that prohibit first-turn automatic confirmation, and route sends through `QQAdapter` only.
- [ ] **Step 5: Add `/nexus relay-confirm <X-task-id>` fallback and run relay/authorization tests.**

### Task 6: Update documentation, contract tests, and version

**Files:**
- Modify: `metadata.yaml`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `tests/test_core.py`

- [ ] **Step 1: Update version to `0.3.0`** and document archive default-off behavior, retention, search, summaries, weekly summaries, cached delivery, and relay confirmation.
- [ ] **Step 2: Add only two concise AGENTS rules** covering archive opt-in and relay preview/explicit confirmation.
- [ ] **Step 3: Extend contract tests** for all retained 13 tools, new archive/summary/relay tools, version, no deprecated API, and no out-of-scope claims.
- [ ] **Step 4: Run the full test suite and inspect the complete diff for runtime files or credentials.**

### Task 7: Final compatibility verification and delivery

**Files:**
- Verify: all repository files and Git state

- [ ] **Step 1: Run `python3 -m compileall .`.**
- [ ] **Step 2: Run `python3 -m unittest discover -s tests -v` and record the exact pass count.**
- [ ] **Step 3: Run `git diff --check`; run `ruff check .` only if Ruff is installed.**
- [ ] **Step 4: Load the plugin through the official AstrBot 4.28.0 package path and verify all old and new tools register, including `get_current_chat_provider_id` and `llm_generate` references.**
- [ ] **Step 5: Confirm no DB, archive rows, exports, cache, local config, credentials, token, or secret is staged; commit with `feat: add group archive summaries and relay` and push `main` without force.**
