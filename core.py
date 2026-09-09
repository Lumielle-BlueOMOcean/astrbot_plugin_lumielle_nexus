"""Core task lifecycle and collection parsing for Lumielle Nexus."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__:
    from .storage import Storage
else:
    from storage import Storage

UTC = timezone.utc


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _as_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text.replace("/", "-"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_run_at_datetime(value: str | datetime, timezone_name: str) -> datetime:
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
    return parsed.astimezone(UTC)


def parse_run_at(value: str | datetime, timezone_name: str) -> str:
    return parse_run_at_datetime(value, timezone_name).isoformat(timespec="seconds")


def _parse_time_of_day(value: str) -> time:
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("时间必须使用 HH:MM 格式")
    return datetime.strptime(text, "%H:%M").time()


def _parse_date(value: str | date | None, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} 请使用 YYYY-MM-DD 格式") from exc


def _coerce_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是非负整数")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        number = int(value.strip())
    else:
        raise ValueError(f"{label} 必须是非负整数")
    if number < 0:
        raise ValueError(f"{label} 必须是非负整数")
    return number


def _normalize_weekdays(weekdays: list[int]) -> list[int]:
    if not isinstance(weekdays, list) or not weekdays:
        raise ValueError("weekdays 至少需要一个星期几（1=周一，7=周日）")
    normalized: set[int] = set()
    for weekday in weekdays:
        if isinstance(weekday, bool):
            raise ValueError("weekdays 必须是 1 到 7 的整数")
        if isinstance(weekday, int):
            number = weekday
        elif isinstance(weekday, str) and re.fullmatch(r"[1-7]", weekday.strip()):
            number = int(weekday.strip())
        else:
            raise ValueError("weekdays 必须是 1 到 7 的整数")
        if number < 1 or number > 7:
            raise ValueError("weekdays 必须是 1 到 7 的整数")
        normalized.add(number)
    return sorted(normalized)


def next_weekly_occurrence(
    after: str | datetime,
    weekdays: list[int],
    time_of_day: str,
    timezone_name: str,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> datetime | None:
    """Return the next strictly-later weekly local occurrence as UTC."""
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无效时区：{timezone_name}") from exc
    normalized_weekdays = _normalize_weekdays(weekdays)
    local_time = _parse_time_of_day(time_of_day)
    first_date = _parse_date(start_date, "start_date")
    last_date = _parse_date(end_date, "end_date")
    if first_date and last_date and first_date > last_date:
        raise ValueError("start_date 不能晚于 end_date")
    after_utc = _as_utc(after)
    after_local = after_utc.astimezone(local_zone)
    first_offset = 0
    if first_date and first_date > after_local.date():
        first_offset = (first_date - after_local.date()).days
    for day_offset in range(first_offset, first_offset + 8):
        candidate_date = after_local.date() + timedelta(days=day_offset)
        if candidate_date < (first_date or candidate_date):
            continue
        if last_date and candidate_date > last_date:
            break
        if candidate_date.isoweekday() not in normalized_weekdays:
            continue
        candidate = datetime.combine(candidate_date, local_time, tzinfo=local_zone)
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc <= after_utc:
            continue
        return candidate_utc
    return None


def next_course_reminder_occurrence(
    after: str | datetime,
    weekdays: list[int],
    start_time: str,
    remind_before_minutes: int,
    timezone_name: str,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> datetime | None:
    """Return a course reminder, preserving course-date bounds across midnight."""
    remind_before_minutes = _coerce_nonnegative_int(
        remind_before_minutes, "remind_before_minutes",
    )
    after_utc = _as_utc(after)
    course_after = after_utc + timedelta(minutes=remind_before_minutes)
    course_start = next_weekly_occurrence(
        course_after,
        weekdays,
        start_time,
        timezone_name,
        start_date,
        end_date,
    )
    if course_start is None:
        return None
    return course_start - timedelta(minutes=remind_before_minutes)


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
    return result


def format_local_time(
    value: str | datetime | None,
    timezone_name: str,
    timespec: str = "seconds",
) -> str:
    """Render a stored UTC timestamp in the configured local timezone."""
    if not value:
        return ""
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无效时区：{timezone_name}") from exc
    parsed = value if isinstance(value, datetime) else None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    format_string = "%Y-%m-%d %H:%M" if timespec == "minutes" else "%Y-%m-%d %H:%M:%S"
    return parsed.astimezone(local_zone).strftime(format_string)


def collection_member_stats(
    members: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    self_id: str | None = None,
) -> dict[str, Any]:
    """Return member/submission sets using one consistent eligibility rule."""
    excluded_id = str(self_id or "").strip()
    eligible_members: dict[str, dict[str, Any]] = {}
    for member in members:
        member_id = str(member.get("user_id", "")).strip()
        if not member_id or member_id == excluded_id or member.get("is_robot") is True:
            continue
        eligible_members[member_id] = member
    eligible_ids = set(eligible_members)
    submitted_ids = {
        str(entry.get("sender_id", "")).strip()
        for entry in entries
        if str(entry.get("sender_id", "")).strip() in eligible_ids
    }
    return {
        "eligible_members": eligible_members,
        "eligible_ids": eligible_ids,
        "submitted_ids": submitted_ids,
        "missing_ids": eligible_ids - submitted_ids,
    }


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
    def _now_utc(now: datetime | None = None) -> datetime:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(UTC)

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
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            raise ValueError("提醒内容不能为空")
        async with self.lock:
            binding = self._resolve_binding(group, platform_id)
            current = self._now_utc(now)
            run_at_dt = parse_run_at_datetime(run_at, self.timezone_name)
            if run_at_dt < current - timedelta(seconds=60):
                raise ValueError("提醒时间不能早于当前时间超过 60 秒")
            now_iso = current.isoformat(timespec="seconds")
            return self.storage.create_task(
                task_id=self._task_id("R"),
                task_type="REMINDER",
                status="PENDING",
                group_id=binding["group_id"],
                group_alias=binding["alias"],
                platform_id=platform_id,
                creator_id=str(creator_id),
                creator_private_origin=str(creator_private_origin),
                created_at=now_iso,
                run_at=run_at_dt.isoformat(timespec="seconds"),
                payload={"message": message, "mention_all": bool(mention_all)},
            )

    def _create_schedule_parent(
        self,
        *,
        task_id: str,
        task_type: str,
        group: str,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
        created_at: str,
        run_at: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        binding = self._resolve_binding(group, platform_id)
        return self.storage.create_task(
            task_id=task_id,
            task_type=task_type,
            status="ACTIVE",
            group_id=binding["group_id"],
            group_alias=binding["alias"],
            platform_id=platform_id,
            creator_id=str(creator_id),
            creator_private_origin=str(creator_private_origin),
            created_at=created_at,
            run_at=run_at,
            payload=payload,
        )

    @staticmethod
    def _date_text(value: str | date | None) -> str:
        parsed = _parse_date(value, "日期")
        return parsed.isoformat() if parsed else ""

    @staticmethod
    def _normalize_offsets(offsets: list[int]) -> list[int]:
        if not isinstance(offsets, list) or len(offsets) > 20:
            raise ValueError("remind_before_minutes 最多支持 20 个提醒时间")
        normalized: set[int] = set()
        for offset in offsets:
            value = _coerce_nonnegative_int(offset, "提醒提前分钟数")
            if value > 366 * 24 * 60:
                raise ValueError("提醒提前分钟数必须在 0 到 366 天之间")
            normalized.add(value)
        return sorted(normalized)

    async def create_recurring_reminder(
        self,
        group: str,
        weekdays: list[int],
        time_of_day: str,
        message: str,
        mention_all: bool,
        start_date: str = "",
        end_date: str = "",
        platform_id: str = "",
        creator_id: str = "",
        creator_private_origin: str = "",
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            raise ValueError("提醒内容不能为空")
        normalized_weekdays = _normalize_weekdays(weekdays)
        _parse_time_of_day(time_of_day)
        start_text = self._date_text(start_date)
        end_text = self._date_text(end_date)
        current = self._now_utc(now)
        next_run = next_weekly_occurrence(
            current,
            normalized_weekdays,
            time_of_day,
            self.timezone_name,
            start_text,
            end_text,
        )
        if next_run is None:
            raise ValueError("没有符合日期范围的下一次提醒")
        async with self.lock:
            now_iso = current.isoformat(timespec="seconds")
            return self._create_schedule_parent(
                task_id=self._task_id("S"),
                task_type="RECURRING",
                group=group,
                platform_id=platform_id,
                creator_id=creator_id,
                creator_private_origin=creator_private_origin,
                created_at=now_iso,
                run_at=next_run.isoformat(timespec="seconds"),
                payload={
                    "weekdays": normalized_weekdays,
                    "time_of_day": str(time_of_day).strip(),
                    "message": message,
                    "mention_all": bool(mention_all),
                    "start_date": start_text,
                    "end_date": end_text,
                },
            )

    async def create_course(
        self,
        group: str,
        course_name: str,
        weekdays: list[int],
        start_time: str,
        location: str,
        remind_before_minutes: int,
        start_date: str = "",
        end_date: str = "",
        mention_all: bool = False,
        platform_id: str = "",
        creator_id: str = "",
        creator_private_origin: str = "",
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        course_name = str(course_name or "").strip()
        if not course_name:
            raise ValueError("课程名称不能为空")
        normalized_weekdays = _normalize_weekdays(weekdays)
        _parse_time_of_day(start_time)
        offset = _coerce_nonnegative_int(
            remind_before_minutes, "remind_before_minutes",
        )
        start_text = self._date_text(start_date)
        end_text = self._date_text(end_date)
        current = self._now_utc(now)
        next_run = next_course_reminder_occurrence(
            current,
            normalized_weekdays,
            start_time,
            offset,
            self.timezone_name,
            start_text,
            end_text,
        )
        if next_run is None:
            raise ValueError("没有符合日期范围的下一次课程提醒")
        async with self.lock:
            now_iso = current.isoformat(timespec="seconds")
            return self._create_schedule_parent(
                task_id=self._task_id("K"),
                task_type="COURSE",
                group=group,
                platform_id=platform_id,
                creator_id=creator_id,
                creator_private_origin=creator_private_origin,
                created_at=now_iso,
                run_at=next_run.isoformat(timespec="seconds"),
                payload={
                    "course_name": course_name,
                    "weekdays": normalized_weekdays,
                    "start_time": str(start_time).strip(),
                    "location": str(location or "").strip(),
                    "remind_before_minutes": offset,
                    "start_date": start_text,
                    "end_date": end_text,
                    "mention_all": bool(mention_all),
                    "message": self._course_message(
                        course_name, start_time, location, offset,
                    ),
                },
            )

    @staticmethod
    def _course_message(
        course_name: str, start_time: str, location: str, offset: int,
    ) -> str:
        lines = ["【课程提醒】", course_name, f"时间：{str(start_time).strip()}"]
        if str(location or "").strip():
            lines.append(f"地点：{str(location).strip()}")
        lines.append(f"{offset} 分钟后上课。" if offset else "现在开始上课。")
        return "\n".join(lines)

    async def create_ddl(
        self,
        group: str,
        title: str,
        deadline: str | datetime,
        remind_before_minutes: list[int],
        message: str = "",
        mention_all: bool = False,
        platform_id: str = "",
        creator_id: str = "",
        creator_private_origin: str = "",
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise ValueError("DDL 标题不能为空")
        offsets = self._normalize_offsets(remind_before_minutes)
        current = self._now_utc(now)
        deadline_dt = parse_run_at_datetime(deadline, self.timezone_name)
        if deadline_dt <= current:
            raise ValueError("截止时间必须在未来")
        deadline_iso = deadline_dt.isoformat(timespec="seconds")
        message = str(message or "").strip() or (
            f"【DDL提醒】{title}\n"
            f"截止时间：{format_local_time(deadline_dt, self.timezone_name, 'minutes')}"
        )
        skipped_offsets = [
            offset for offset in offsets
            if deadline_dt - timedelta(minutes=offset) < current
        ]
        async with self.lock:
            now_iso = current.isoformat(timespec="seconds")
            binding = self._resolve_binding(group, platform_id)
            payload = {
                "title": title,
                "deadline": deadline_iso,
                "remind_before_minutes": offsets,
                "skipped_offsets": skipped_offsets,
                "message": message,
                "mention_all": bool(mention_all),
            }
            task = self.storage.create_task(
                task_id=self._task_id("D"),
                task_type="DDL",
                status="ACTIVE",
                group_id=binding["group_id"],
                group_alias=binding["alias"],
                platform_id=platform_id,
                creator_id=str(creator_id),
                creator_private_origin=str(creator_private_origin),
                created_at=now_iso,
                run_at=deadline_iso,
                payload=payload,
            )
            for offset in offsets:
                child_run = deadline_dt - timedelta(minutes=offset)
                if child_run < current:
                    continue
                self.storage.create_child_reminder(
                    task_id=self._task_id("R"),
                    parent_id=task["id"],
                    occurrence_key=f"ddl:-{offset}",
                    group_id=binding["group_id"],
                    group_alias=binding["alias"],
                    platform_id=platform_id,
                    creator_id=str(creator_id),
                    creator_private_origin=str(creator_private_origin),
                    created_at=now_iso,
                    run_at=child_run.isoformat(timespec="seconds"),
                    payload={
                        "message": message,
                        "mention_all": bool(mention_all),
                        "kind": "ddl",
                        "offset_minutes": offset,
                    },
                )
            return task

    async def list_tasks(
        self,
        platform_id: str,
        group: str | None = None,
        include_children: bool = False,
    ) -> list[dict[str, Any]]:
        async with self.lock:
            group_id = None
            if group:
                group_id = self._resolve_binding(group, platform_id)["group_id"]
            return self.storage.list_tasks(platform_id, group_id, include_children)

    async def get_task(self, task_id: str, platform_id: str) -> dict[str, Any]:
        async with self.lock:
            task = self.storage.get_task(str(task_id).strip())
            if task is None or task["platform_id"] != platform_id:
                raise KeyError(f"任务不存在：{task_id}")
            return task

    async def cancel_task(self, task_id: str, platform_id: str) -> dict[str, Any]:
        async with self.lock:
            task = self.storage.get_task(str(task_id).strip())
            if task is None or task["platform_id"] != platform_id:
                raise KeyError(f"任务不存在：{task_id}")
            if task["type"] == "COLLECTION" and task["status"] == "ACTIVE":
                raise ValueError(
                    "进行中的信息收集不能通过 cancel 结束。请使用 collect-stop / "
                    "nexus_stop_collection，以便生成并保存统计结果。",
                )
            if task["type"] == "REMINDER":
                if task["status"] != "PENDING":
                    raise ValueError(f"任务当前不能取消：{task['status']}")
            elif task["type"] in {"DDL", "RECURRING", "COURSE"}:
                if task["status"] != "ACTIVE":
                    raise ValueError(f"任务当前不能取消：{task['status']}")
            else:
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
            if task["type"] != "COLLECTION":
                raise ValueError(f"该任务不是信息收集任务：{task_id}")
            entries = self.storage.list_entries(task["id"])
            return {"task": task, "entries": entries, "submitted_count": len(entries)}

    async def schedule_collection_chase(
        self,
        task_id: str,
        run_at: str | datetime,
        message: str,
        repeat_interval_minutes: int,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
    ) -> dict[str, Any]:
        repeat_interval_minutes = _coerce_nonnegative_int(
            0 if repeat_interval_minutes is None else repeat_interval_minutes,
            "重复催办间隔",
        )
        if repeat_interval_minutes and not 60 <= repeat_interval_minutes <= 7 * 24 * 60:
            raise ValueError("重复催办间隔必须为 0 或至少 60 分钟，且不超过 7 天")
        async with self.lock:
            collection = self.storage.get_task(str(task_id).strip())
            if (
                collection is None
                or collection["platform_id"] != platform_id
            ):
                raise KeyError(f"任务不存在：{task_id}")
            if collection["type"] != "COLLECTION" or collection["status"] != "ACTIVE":
                raise ValueError("只能为 ACTIVE COLLECTION 安排催办")
            current = self._now_utc()
            run_at_dt = parse_run_at_datetime(run_at, self.timezone_name)
            if run_at_dt < current - timedelta(seconds=60):
                raise ValueError("催办时间不能早于当前时间超过 60 秒")
            payload = json.loads(collection["payload"])
            title = str(payload.get("title") or collection["group_alias"])
            clean_message = str(message or "").strip() or (
                f"以下同学尚未完成「{title}」，请及时提交。"
            )
            run_at_iso = run_at_dt.isoformat(timespec="seconds")
            return self.storage.create_child_reminder(
                task_id=self._task_id("R"),
                parent_id=collection["id"],
                occurrence_key=f"chase:{run_at_iso}",
                group_id=collection["group_id"],
                group_alias=collection["group_alias"],
                platform_id=platform_id,
                creator_id=str(creator_id or collection["creator_id"]),
                creator_private_origin=str(
                    creator_private_origin or collection["creator_private_origin"]
                ),
                created_at=current.isoformat(timespec="seconds"),
                run_at=run_at_iso,
                payload={
                    "kind": "collection_chase",
                    "collection_task_id": collection["id"],
                    "message": clean_message,
                    "repeat_interval_minutes": repeat_interval_minutes,
                    "mention_all": False,
                },
            )

    async def stop_collection(self, task_id: str, platform_id: str) -> dict[str, Any]:
        async with self.lock:
            task = self.storage.transition_collection_to_processing(
                str(task_id).strip(), platform_id, utc_now_iso(),
            )
            return {"task": task, "entries": self.storage.list_entries(task["id"])}

    def _next_schedule_occurrence(
        self,
        parent: dict[str, Any],
        after: datetime,
    ) -> datetime | None:
        payload = json.loads(parent.get("payload") or "{}")
        if parent["type"] == "RECURRING":
            return next_weekly_occurrence(
                after,
                payload["weekdays"],
                payload["time_of_day"],
                self.timezone_name,
                payload.get("start_date") or None,
                payload.get("end_date") or None,
            )
        if parent["type"] == "COURSE":
            return next_course_reminder_occurrence(
                after,
                payload["weekdays"],
                payload["start_time"],
                int(payload["remind_before_minutes"]),
                self.timezone_name,
                payload.get("start_date") or None,
                payload.get("end_date") or None,
            )
        return None

    async def materialize_due_schedules(
        self,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        async with self.lock:
            current = self._now_utc(now)
            current_iso = current.isoformat(timespec="seconds")
            created: list[dict[str, Any]] = []
            for parent in self.storage.list_schedule_parents():
                if not parent.get("run_at"):
                    continue
                run_at = parse_run_at_datetime(parent["run_at"], "UTC")
                if run_at > current:
                    continue
                if parent["type"] == "DDL":
                    self.storage.update_task(
                        parent["id"],
                        status="COMPLETED",
                        updated_at=current_iso,
                        finished_at=current_iso,
                    )
                    continue

                within_grace = (current - run_at).total_seconds() <= 120
                next_after = run_at if within_grace else current
                next_run = self._next_schedule_occurrence(parent, next_after)
                if within_grace:
                    payload = json.loads(parent["payload"])
                    occurrence_key = (
                        f"{parent['type'].lower()}:"
                        f"{run_at.astimezone(self.timezone).isoformat(timespec='minutes')}"
                    )
                    child = self.storage.create_child_reminder(
                        task_id=self._task_id("R"),
                        parent_id=parent["id"],
                        occurrence_key=occurrence_key,
                        group_id=parent["group_id"],
                        group_alias=parent["group_alias"],
                        platform_id=parent["platform_id"],
                        creator_id=parent["creator_id"],
                        creator_private_origin=parent["creator_private_origin"],
                        created_at=current_iso,
                        run_at=run_at.isoformat(timespec="seconds"),
                        payload={
                            "message": payload["message"],
                            "mention_all": bool(payload.get("mention_all")),
                            "kind": "schedule",
                            "parent_type": parent["type"],
                            "occurrence_key": occurrence_key,
                        },
                    )
                    created.append(child)
                if next_run is None:
                    self.storage.update_task(
                        parent["id"],
                        status="COMPLETED",
                        updated_at=current_iso,
                        finished_at=current_iso,
                    )
                else:
                    self.storage.update_task(
                        parent["id"],
                        run_at=next_run.isoformat(timespec="seconds"),
                        updated_at=current_iso,
                    )
            return created

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
            delay_seconds = {
                1: 30,
                2: 120,
                3: 300,
            }.get(retries, min(900, 30 * (2 ** (retries - 1))))
            retry_run_at = (
                datetime.now(UTC) + timedelta(seconds=delay_seconds)
            ).isoformat(timespec="seconds")
            return self.storage.update_task(
                task_id,
                status="FAILED" if final else "PENDING",
                run_at=None if final else retry_run_at,
                retry_count=retries,
                last_error=str(error)[:1000],
                updated_at=now,
                finished_at=now if final else None,
            )
