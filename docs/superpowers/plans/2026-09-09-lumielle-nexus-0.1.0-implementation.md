# Lumielle Nexus 0.1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a simple AstrBot 4.28.0-compatible aiocqhttp plugin with persistent group bindings, reminders, collection tasks, status/stop/export, and protected private control.

**Architecture:** `main.py` owns AstrBot handlers and delegates to `TaskManager` in `core.py`; `Storage` owns SQLite; `QQAdapter` owns aiocqhttp/OneBot calls; `exporter.py` owns XLSX. One scheduler task runs in the plugin lifecycle and all state changes are guarded by a single asyncio lock.

**Tech Stack:** Python 3 stdlib (`asyncio`, `sqlite3`, `json`, `datetime`, `zoneinfo`, `pathlib`, `unittest`) plus `openpyxl`.

**Spec:** `docs/superpowers/specs/2026-09-09-lumielle-nexus-0.1.0-design.md`

## Global Constraints

- Target AstrBot compatibility: `>=4.28.0,<5`.
- Officially support only `aiocqhttp` (QQ + OneBot v11/NapCat).
- Runtime data must be under `data/plugin_data/astrbot_plugin_lumielle_nexus/`.
- Do not use deprecated `register_star` / `@register(...)`.
- Control commands and tools require private chat plus AstrBot admin or configured `operator_ids`.
- Same platform/group has at most one active collection task.
- Same collection task and sender use one effective entry via upsert.
- Failed member-list or file-upload calls must not destroy a usable task/export.
- No kick, mute, whole-ban, group admin, WebUI, history archive, recurring schedule, complex RRULE, OCR, or free-text LLM extraction.
- Never commit SQLite files, exports, caches, secrets, tokens, or local AstrBot configuration.

### Task 1: Repository and test harness

**Files:**
- Create: `.gitignore`
- Create: `requirements.txt`
- Create: `tests/test_core.py`

**Interfaces:**
- Produces a clean `unittest` entry point that imports local `storage`, `core`, and `exporter` modules once they exist.

- [ ] **Step 1: Write the failing smoke tests**

  Add tests for schema initialization, binding replacement/listing, reminder create/list/cancel, collection creation, field parsing/upsert, and XLSX sheet names. The tests use `TemporaryDirectory`, do not touch AstrBot runtime data, and assert user-visible behavior rather than implementation calls.

- [ ] **Step 2: Run the tests to verify the missing-module failure**

  Run `python3 -m unittest discover -s tests -v`. Expected: import failure because production modules have not yet been created.

- [ ] **Step 3: Add dependency and ignore rules**

  Put only `openpyxl` in `requirements.txt`; ignore `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `*.db`, `*.db-*`, `exports/`, `.venv/`, and local AstrBot config/data paths.

### Task 2: SQLite storage and pure core collection/reminder behavior

**Files:**
- Create: `storage.py`
- Create: `core.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- `Storage(data_dir: Path)` with `close()`, binding CRUD, task CRUD/claim/update, and collection entry upsert/list methods.
- `TaskManager(storage, timezone_name, max_retry_count)` with binding methods, reminder methods, collection methods, `process_collection_message`, status/stop support, and `due_tasks(now)`.

- [ ] **Step 1: Expand failing tests for exact task/entry semantics**

  Assert task IDs begin with `R-`/`C-`, JSON payload fields round-trip, cancelling prevents a task from being due, a single active collection is rejected, and a second sender submission updates the original effective entry.

- [ ] **Step 2: Run the focused tests and confirm they fail**

  Run `python3 -m unittest tests.test_core -v`. Expected: missing `storage`/`core` symbols.

- [ ] **Step 3: Implement schema and storage methods**

  Create `group_bindings`, `tasks`, and `collection_entries` with the required unique keys and indexes. Use parameterized SQL, ISO-8601 UTC timestamps, JSON serialization, and `BEGIN IMMEDIATE` for task claiming and collection start/stop transitions.

- [ ] **Step 4: Implement minimal TaskManager behavior**

  Normalize ids and fields, resolve groups by alias or numeric id, parse `run_at` in the configured timezone, generate readable ids, accept colon-delimited collection submissions, preserve partial values, and upsert entries by `(task_id, sender_id)`.

- [ ] **Step 5: Run focused tests and make them pass**

  Run `python3 -m unittest tests.test_core -v`. Expected: all storage/core tests pass.

### Task 3: XLSX export

**Files:**
- Create: `exporter.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- `export_collection(path, task, entries, members=None) -> Path` writes `统计结果`, `未提交成员`, and `任务信息` and returns the safe output path.

- [ ] **Step 1: Write the failing export assertions**

  Verify all requested fields and metadata appear in the workbook, filenames are sanitized, and missing member-list data creates an explanatory `未提交成员` sheet.

- [ ] **Step 2: Run the export test to verify it fails**

  Run `python3 -m unittest tests.test_core.CollectionExportTests -v`. Expected: `ModuleNotFoundError` for `exporter`.

- [ ] **Step 3: Implement the workbook writer**

  Use `openpyxl`, write headers and rows with plain values, preserve raw submission text, create `exports/`, and sanitize task title to a bounded filename without allowing path separators.

- [ ] **Step 4: Run the export test and make it pass**

  Run `python3 -m unittest tests.test_core.CollectionExportTests -v`. Expected: PASS.

### Task 4: QQ adapter and plugin lifecycle

**Files:**
- Create: `qq_adapter.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- `QQAdapter(context, platform_id)` with `get_group_info`, `get_group_member_list`, `send_group_text`, `send_group_message`, `send_group_at_all`, `send_private_message`, and `upload_private_file`.
- Adapter methods convert OneBot failures to `QQAdapterError` and resolve the requested aiocqhttp platform instance without scattering `call_action` calls.

- [ ] **Step 1: Add adapter contract smoke coverage**

  Use a small fake client to assert action names/arguments and readable exception conversion; no live QQ connection is required.

- [ ] **Step 2: Run the adapter test and verify it fails**

  Run `python3 -m unittest tests.test_core.QQAdapterTests -v`. Expected: missing `qq_adapter` symbols.

- [ ] **Step 3: Implement the adapter**

  Prefer the current AstrBot platform/client access, call OneBot v11 actions with numeric IDs where required, and use OneBot `upload_private_file` for XLSX delivery.

- [ ] **Step 4: Implement plugin data initialization and scheduler hooks**

  Keep scheduler creation in `initialize()`, cancel and await it in `terminate()`, construct data directory with the stable 4.28.0 plugin-data API, and let each scheduler iteration catch/log per-task errors.

- [ ] **Step 5: Run the plugin import smoke**

  Import the plugin with the local AstrBot 4.25.5 source on `PYTHONPATH`; expected: module import succeeds without invoking a live platform.

### Task 5: Commands, LLM tools, authorization, and collection listener

**Files:**
- Create: `main.py`
- Create: `metadata.yaml`
- Create: `_conf_schema.json`
- Create: `README.md`
- Create: `AGENTS.md`
- Modify: `.gitignore`

**Interfaces:**
- `LumielleNexus(Star)` exposes `is_authorized_operator(event)`, lifecycle methods, `/nexus` command group, the required eight LLM tools, and a `GROUP_MESSAGE` listener.
- Every control handler verifies private chat and operator authorization before validating arguments or invoking core/adapter operations.

- [ ] **Step 1: Add source-contract tests**

  Assert metadata values, config defaults, absence of deprecated registration decorators, required tool names, and the command-group/listener decorators.

- [ ] **Step 2: Run source-contract tests to verify they fail**

  Run `python3 -m unittest tests.test_core.PluginContractTests -v`. Expected: missing plugin files or required source strings.

- [ ] **Step 3: Implement the Star plugin**

  Use `from astrbot.api import filter, logger`, `from astrbot.api.star import Context, Star`, and current event/message APIs. Commands cover help, bind, groups, tasks, cancel, collection start/status/stop; tools return concise strings and repeat the auth gate. Listener extracts text/sender/group, ignores bot messages, and sends optional acknowledgements.

- [ ] **Step 4: Add metadata/config/docs and remove runtime artifacts from Git**

  Declare version `0.1.0`, support only `aiocqhttp`, AstrBot `>=4.28.0,<5`, the requested config fields, truthful README limitations, and concise repository rules in `AGENTS.md`.

- [ ] **Step 5: Run the complete local smoke suite**

  Run `python3 -m unittest discover -s tests -v`; expected: all tests pass.

### Task 6: Final verification, commit, and push

**Files:**
- Modify: any implementation files needed by verification findings only.

- [ ] **Step 1: Run syntax verification**

  Run `python3 -m compileall .`; expected: exit 0 and no generated cache files left for commit.

- [ ] **Step 2: Run Ruff if installed**

  If `ruff` exists, run `ruff check .` and fix only issues introduced by this implementation.

- [ ] **Step 3: Run core and import/load verification**

  Run `python3 -m unittest discover -s tests -v` and the local AstrBot import/load smoke. Record that no real QQ/NapCat connection was made.

- [ ] **Step 4: Inspect repository contents and diff**

  Run `git status --short`, `git diff --check`, `git diff --stat`, and explicit searches for `*.db`, `*.db-*`, `exports`, `__pycache__`, tokens, and local config. Do not stage runtime artifacts.

- [ ] **Step 5: Commit and push**

  Commit with `feat: initialize Lumielle Nexus group task orchestrator`, verify the full SHA, then run `git push origin main`. If proxy/network fails, preserve the local commit and report the exact error.
