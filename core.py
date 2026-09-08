"""Core task lifecycle and collection parsing for Lumielle Nexus."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from storage import Storage

UTC = timezone.utc


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_run_at(value: str | datetime, timezone_name: str) -> str:
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无效时区：{timezone_name}") from exc
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        text = text.replace("/", "-")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("run_at 请使用 YYYY-MM-DD HH:MM 或 ISO-8601 时间") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def parse_collection_submission(
    fields: list[str], raw_message: str,
) -> dict[str, str]:
    normalized = {field.strip().casefold(): field.strip() for field in fields}
    result: dict[str, str] = {}
    for line in raw_message.splitlines():
        match = re.match(r"^\s*(.+?)\s*[:：]\s*(.*?)\s*$", line)
        if not match:
            continue
        label = match.group(1).strip().casefold()
        value = match.group(2).strip()
        if label in normalized and value:
            result[normalized[label]] = value
    if not result and len(fields) == 1 and raw_message.strip():
        result[fields[0]] = raw_message.strip()
    return result


class TaskManager:
    def __init__(
        self,
        storage: Storage,
        timezone_name: str = "Asia/Shanghai",
        max_retry_count: int = 3,
    ) -> None:
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"无效时区：{timezone_name}") from exc
        self.timezone_name = timezone_name
        self.max_retry_count = max(0, int(max_retry_count))
        self.storage = storage
        self.lock = asyncio.Lock()
        self.storage.recover_processing_reminders(utc_now_iso())

    def _task_id(self, prefix: str) -> str:
        date_token = datetime.now(self.timezone).strftime("%Y%m%d")
        return self.storage.next_task_id(prefix, date_token)

    @staticmethod
    def _clean_group(group: str) -> str:
        group = str(group or "").strip()
        if not group:
            raise ValueError("请提供群别名或群号")
        return group

    def _resolve_binding(self, group: str, platform_id: str) -> dict[str, Any]:
        binding = self.storage.get_binding(self._clean_group(group), platform_id)
        if binding is None:
            raise ValueError(f"未找到已绑定群：{group}")
        return binding

    async def bind_group(
        self,
        alias: str,
        group_id: str,
        platform_id: str,
        created_by: str,
    ) -> dict[str, Any]:
        alias = str(alias or "").strip()
        group_id = str(group_id or "").strip()
        if not alias or not group_id or not platform_id:
            raise ValueError("群别名、群号和平台 ID 不能为空")
        async with self.lock:
            return self.storage.upsert_binding(
                alias, group_id, platform_id, str(created_by), utc_now_iso(),
            )

    async def list_groups(self, platform_id: str) -> list[dict[str, Any]]:
        async with self.lock:
            return self.storage.list_bindings(platform_id)

    async def create_reminder(
        self,
        group: str,
        run_at: str | datetime,
        message: str,
        mention_all: bool,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
    ) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            raise ValueError("提醒内容不能为空")
        async with self.lock:
            binding = self._resolve_binding(group, platform_id)
            now = utc_now_iso()
            return self.storage.create_task(
                task_id=self._task_id("R"),
                task_type="REMINDER",
                status="PENDING",
                group_id=binding["group_id"],
                group_alias=binding["alias"],
                platform_id=platform_id,
                creator_id=str(creator_id),
                creator_private_origin=str(creator_private_origin),
                created_at=now,
                run_at=parse_run_at(run_at, self.timezone_name),
                payload={"message": message, "mention_all": bool(mention_all)},
            )

    async def list_tasks(
        self,
        platform_id: str,
        group: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.lock:
            group_id = None
            if group:
                group_id = self._resolve_binding(group, platform_id)["group_id"]
            return self.storage.list_tasks(platform_id, group_id)

    async def cancel_task(self, task_id: str, platform_id: str) -> dict[str, Any]:
        async with self.lock:
            task = self.storage.get_task(str(task_id).strip())
            if task is None or task["platform_id"] != platform_id:
                raise KeyError(f"任务不存在：{task_id}")
            if task["status"] not in {"PENDING", "ACTIVE"}:
                raise ValueError(f"任务当前不能取消：{task['status']}")
            return self.storage.cancel_task(task["id"], platform_id, utc_now_iso())

    async def start_collection(
        self,
        group: str,
        title: str,
        fields: list[str],
        announcement: str,
        mention_all: bool,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        clean_fields = [str(field).strip() for field in (fields or []) if str(field).strip()]
        if not title:
            raise ValueError("统计标题不能为空")
        if not clean_fields:
            raise ValueError("至少需要一个统计字段")
        if len(set(field.casefold() for field in clean_fields)) != len(clean_fields):
            raise ValueError("统计字段不能重复")
        async with self.lock:
            binding = self._resolve_binding(group, platform_id)
            now = utc_now_iso()
            payload = {
                "title": title,
                "fields": clean_fields,
                "announcement": str(announcement or "").strip(),
                "mention_all": bool(mention_all),
            }
            return self.storage.create_collection_task(
                task_id=self._task_id("C"),
                group_id=binding["group_id"],
                group_alias=binding["alias"],
                platform_id=platform_id,
                creator_id=str(creator_id),
                creator_private_origin=str(creator_private_origin),
                created_at=now,
                payload=payload,
            )

    async def process_collection_message(
        self,
        platform_id: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        raw_message: str,
    ) -> dict[str, Any] | None:
        async with self.lock:
            task = self.storage.get_active_collection(platform_id, str(group_id))
            if task is None:
                return None
            payload = json.loads(task["payload"])
            parsed = parse_collection_submission(payload["fields"], raw_message)
            if not parsed:
                return None
            previous = self.storage.get_entry(task["id"], str(sender_id))
            if previous:
                merged = json.loads(previous["parsed_data"])
                merged.update(parsed)
            else:
                merged = parsed
            entry = self.storage.upsert_entry(
                task["id"],
                str(sender_id),
                str(sender_name or sender_id),
                str(raw_message),
                merged,
                utc_now_iso(),
            )
            missing = [field for field in payload["fields"] if not merged.get(field)]
            entry["parsed_data"] = merged
            return {
                "task": task,
                "entry": entry,
                "parsed_data": merged,
                "missing": missing,
            }

    async def collection_status(self, task_id: str, platform_id: str) -> dict[str, Any]:
        async with self.lock:
            task = self.storage.get_task(str(task_id).strip())
            if task is None or task["platform_id"] != platform_id:
                raise KeyError(f"任务不存在：{task_id}")
            entries = self.storage.list_entries(task["id"])
            return {"task": task, "entries": entries, "submitted_count": len(entries)}

    async def stop_collection(self, task_id: str, platform_id: str) -> dict[str, Any]:
        async with self.lock:
            task = self.storage.transition_collection_to_processing(
                str(task_id).strip(), platform_id, utc_now_iso(),
            )
            return {"task": task, "entries": self.storage.list_entries(task["id"])}

    async def complete_collection(
        self,
        task_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.lock:
            return self.storage.update_task(
                task_id,
                status="COMPLETED",
                result=result,
                updated_at=utc_now_iso(),
                finished_at=utc_now_iso(),
            )

    async def fail_collection(self, task_id: str, error: str) -> dict[str, Any]:
        async with self.lock:
            return self.storage.update_task(
                task_id,
                status="FAILED",
                last_error=str(error)[:1000],
                updated_at=utc_now_iso(),
                finished_at=utc_now_iso(),
            )

    async def due_tasks(self, now: datetime | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            current = now or datetime.now(UTC)
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            return self.storage.claim_due_tasks(
                current.astimezone(UTC).isoformat(timespec="seconds"),
            )

    async def finish_reminder(self, task_id: str, success: bool, error: str = "") -> dict[str, Any]:
        async with self.lock:
            task = self.storage.get_task(task_id)
            if task is None:
                raise KeyError(f"任务不存在：{task_id}")
            now = utc_now_iso()
            if success:
                return self.storage.update_task(
                    task_id,
                    status="COMPLETED",
                    updated_at=now,
                    finished_at=now,
                )
            retries = int(task["retry_count"]) + 1
            final = retries > self.max_retry_count
            return self.storage.update_task(
                task_id,
                status="FAILED" if final else "PENDING",
                retry_count=retries,
                last_error=str(error)[:1000],
                updated_at=now,
                finished_at=now if final else None,
            )
