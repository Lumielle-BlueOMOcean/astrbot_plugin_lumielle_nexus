"""Persistent native QQ group-message poll business logic."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from .core import parse_run_at_datetime
    from .storage import Storage
except ImportError:
    from core import parse_run_at_datetime
    from storage import Storage


class PollError(ValueError):
    """Readable validation or lifecycle error returned to poll callers."""


class PollClosedError(PollError):
    """Raised when a vote arrives after the poll is no longer open."""


POLL_MAX_OPTIONS = 50


_REPLY_SPLITTER = re.compile(r"[\s,，、/|;；+]+")
_FORBIDDEN_REPLY_CHARS = set(",，、/|;；+")
_SEMANTIC_INTENTS = ("我选", "我投", "还是选", "就选", "更想", "倾向", "我觉得")
_ORDINALS = ("第一个", "第二个", "第三个", "第1个", "第2个", "第3个", "第1项", "第2项", "第3项")
_POLL_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])P-\d{8}-\d{3}(?![A-Za-z0-9])", re.IGNORECASE)
_NUMERIC_SELF_CHOICE_PATTERN = re.compile(
    r"(?:我选|我投|还是选|就选|更想|倾向)\s*(?:了|是)?\s*\d+(?:号|项|个)?",
)


def normalize_poll_reply(value: str) -> str:
    """Normalize an exact poll reply without changing its semantic content."""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _validated_reply_keys(options: list[str], reply_keys: list[str] | None) -> list[str]:
    if reply_keys in (None, []):
        keys = [str(index) for index in range(1, len(options) + 1)]
    elif not isinstance(reply_keys, list) or len(reply_keys) != len(options):
        raise PollError("reply_keys 必须与选项数量一致。")
    else:
        keys = []
        for value in reply_keys:
            key = str(value or "").strip()
            if not 1 <= len(key) <= 32:
                raise PollError("每个投票回复 key 必须是 1 到 32 个字符。")
            if any(char.isspace() or char in _FORBIDDEN_REPLY_CHARS for char in key):
                raise PollError("投票回复 key 不能包含空白或分隔符。")
            if normalize_poll_reply(key) in {"投票", "vote"}:
                raise PollError("投票回复 key 不能使用保留词。")
            keys.append(key)
    normalized_keys = [normalize_poll_reply(key) for key in keys]
    if len(set(normalized_keys)) != len(normalized_keys):
        raise PollError("投票回复 key 不能重复。")
    return keys


def _build_options(options: list[str], reply_keys: list[str] | None) -> list[dict[str, Any]]:
    if not isinstance(options, list) or not 2 <= len(options) <= POLL_MAX_OPTIONS:
        raise PollError(f"投票必须有 2 到 {POLL_MAX_OPTIONS} 个选项。")
    labels: list[str] = []
    seen_labels: set[str] = set()
    for option in options:
        label = str(option or "").strip()
        if not 1 <= len(label) <= 120:
            raise PollError("每个投票选项必须是 1 到 120 个字符。")
        normalized = normalize_poll_reply(label)
        if normalized in seen_labels:
            raise PollError("投票选项不能重复。")
        seen_labels.add(normalized)
        labels.append(label)
    keys = _validated_reply_keys(labels, reply_keys)
    aliases: dict[str, int] = {}
    built: list[dict[str, Any]] = []
    for index, (label, reply_key) in enumerate(zip(labels, keys, strict=True), start=1):
        option = {"option_id": index, "position": index, "reply_key": reply_key, "label": label}
        for alias in (str(index), label, reply_key):
            normalized = normalize_poll_reply(alias)
            prior = aliases.get(normalized)
            if prior is not None and prior != index:
                raise PollError("投票选项的位置、标签和 reply key 不能产生歧义。")
            aliases[normalized] = index
        built.append(option)
    return built


def parse_poll_message(message: str, poll: dict[str, Any]) -> list[int] | None:
    """Parse exact positions, labels, or reply keys for one poll."""
    text = str(message or "").strip()
    if not text:
        return None
    options = list(poll.get("options") or [])
    aliases: dict[str, int] = {}
    for option in options:
        option_id = int(option["option_id"])
        for alias in (str(option["position"]), str(option.get("reply_key", "")), option["label"]):
            normalized = normalize_poll_reply(alias)
            if normalized:
                aliases[normalized] = option_id
    whole = aliases.get(normalize_poll_reply(text))
    if whole is not None:
        return [whole]
    parts = [part for part in _REPLY_SPLITTER.split(text) if part]
    if len(parts) <= 1:
        return None
    choices: list[int] = []
    for part in parts:
        choice = aliases.get(normalize_poll_reply(part))
        if choice is None:
            return None
        if choice not in choices:
            choices.append(choice)
    if not choices:
        return None
    if not bool(poll.get("multiple_choice")) and len(choices) != 1:
        raise PollError("单选投票只能选择一个选项。")
    if len(choices) > int(poll.get("max_choices", 1)):
        raise PollError(f"最多选择 {poll['max_choices']} 个选项。")
    return choices


def is_explicit_poll_signal(message: str) -> bool:
    """Return whether a message explicitly refers to the poll workflow."""
    folded = normalize_poll_reply(message)
    return bool(
        "投票" in folded
        or re.search(r"(?<![a-z])vote(?![a-z])", folded)
        or _POLL_ID_PATTERN.search(folded)
    )


def is_poll_semantic_candidate(message: str, polls: list[dict[str, Any]]) -> bool:
    """Return whether a bounded group message plausibly refers to a poll."""
    text = str(message or "").strip()
    if not 1 <= len(text) <= 160:
        return False
    folded = normalize_poll_reply(text)
    if is_explicit_poll_signal(folded) or any(term in folded for term in _ORDINALS):
        return True
    has_option_clue = False
    for poll in polls:
        poll_id = normalize_poll_reply(str(poll.get("id", "")))
        if poll_id and poll_id in folded:
            return True
        for option in poll.get("options") or []:
            reply_key = normalize_poll_reply(str(option.get("reply_key", "")))
            label = normalize_poll_reply(str(option.get("label", "")))
            if reply_key and not reply_key.isdecimal() and reply_key in folded:
                has_option_clue = True
            if label and not label.isdecimal() and label in folded:
                has_option_clue = True
    has_self_choice = any(term in folded for term in _SEMANTIC_INTENTS)
    return bool(
        _NUMERIC_SELF_CHOICE_PATTERN.search(folded)
        or (has_self_choice and has_option_clue)
    )


def validate_semantic_vote_candidate(
    candidate: Any,
    option_count: int,
    multiple_choice: bool,
    max_choices: int,
    message: str,
) -> list[int]:
    """Validate a model classifier result before it can become a ballot."""
    if not isinstance(candidate, dict) or candidate.get("status") != "vote":
        raise PollError("语义投票结果不是明确投票。")
    choices = candidate.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PollError("语义投票结果缺少选项。")
    if any(isinstance(choice, bool) or not isinstance(choice, int) for choice in choices):
        raise PollError("语义投票选项必须是整数。")
    if len(set(choices)) != len(choices) or any(choice < 1 or choice > option_count for choice in choices):
        raise PollError("语义投票选项无效。")
    if not multiple_choice and len(choices) != 1:
        raise PollError("单选投票只能选择一个选项。")
    if len(choices) > max_choices:
        raise PollError("语义投票超出最多可选数量。")
    try:
        confidence = float(candidate.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise PollError("语义投票缺少有效置信度。") from exc
    evidence = str(candidate.get("evidence") or "").strip()
    if not 0.90 <= confidence <= 1.0 or not evidence or evidence not in str(message):
        raise PollError("语义投票缺少足够置信度或原文证据。")
    return choices


class PollService:
    """Small async facade over Storage; the lock serializes vote transitions."""

    def __init__(self, storage: Storage, timezone_name: str = "Asia/Shanghai") -> None:
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"无效时区：{timezone_name}") from exc
        self.timezone_name = timezone_name
        self.storage = storage
        self.lock = asyncio.Lock()

    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(UTC)

    async def create_poll(
        self,
        group: str,
        title: str,
        options: list[str],
        description: str = "",
        deadline: str | datetime = "",
        multiple_choice: bool = False,
        max_choices: int = 0,
        allow_change: bool = True,
        auto_publish_result: bool = True,
        reply_keys: list[str] | None = None,
        semantic_fallback: bool = True,
        ai_provider_id: str = "",
        platform_id: str = "",
        creator_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        clean_description = str(description or "").strip()
        if not 1 <= len(clean_title) <= 200:
            raise PollError("投票标题必须是 1 到 200 个字符。")
        if len(clean_description) > 1000:
            raise PollError("投票说明最多 1000 个字符。")
        built_options = _build_options(options, reply_keys)
        if not isinstance(multiple_choice, bool):
            raise PollError("multiple_choice 必须是布尔值。")
        if isinstance(max_choices, bool):
            raise PollError("max_choices 必须是整数。")
        try:
            requested_max = int(max_choices or 0)
        except (TypeError, ValueError) as exc:
            raise PollError("max_choices 必须是整数。") from exc
        if not multiple_choice:
            requested_max = 1
        elif requested_max == 0:
            requested_max = len(built_options)
        if not 1 <= requested_max <= len(built_options):
            raise PollError("max_choices 必须在 1 到选项数之间。")
        if not isinstance(allow_change, bool) or not isinstance(auto_publish_result, bool):
            raise PollError("allow_change 和 auto_publish_result 必须是布尔值。")
        if not isinstance(semantic_fallback, bool):
            raise PollError("semantic_fallback 必须是布尔值。")
        current = self._now(now)
        deadline_at = None
        if str(deadline or "").strip():
            deadline_at = parse_run_at_datetime(deadline, self.timezone_name)
            if deadline_at <= current:
                raise PollError("投票截止时间必须在未来。")
        async with self.lock:
            binding = self.storage.get_binding(str(group or "").strip(), platform_id)
            if binding is None:
                raise PollError(f"未找到已绑定群：{group}")
            created_at = current.isoformat(timespec="seconds")
            date_token = current.astimezone(self.timezone).strftime("%Y%m%d")
            poll_id = self.storage.next_poll_id(date_token)
            poll = {
                "id": poll_id,
                "platform_id": str(platform_id),
                "group_id": binding["group_id"],
                "group_alias": binding["alias"],
                "creator_id": str(creator_id),
                "title": clean_title,
                "description": clean_description,
                "status": "OPEN",
                "multiple_choice": multiple_choice,
                "max_choices": requested_max,
                "allow_change": allow_change,
                "semantic_fallback": semantic_fallback,
                "ai_provider_id": str(ai_provider_id or ""),
                "auto_publish_result": auto_publish_result,
                "deadline_at": deadline_at.isoformat(timespec="seconds") if deadline_at else None,
                "created_at": created_at,
                "updated_at": created_at,
            }
            created = self.storage.create_poll(poll, built_options)
            return self._view(created)

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        poll = dict(row)
        for key in ("multiple_choice", "allow_change", "semantic_fallback", "auto_publish_result", "announcement_sent", "result_published"):
            poll[key] = bool(poll.get(key))
        poll["options"] = self.storage.get_poll_options(poll["id"])
        if poll.get("deadline_at"):
            deadline = datetime.fromisoformat(str(poll["deadline_at"]))
            poll["deadline_display"] = deadline.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M")
        else:
            poll["deadline_display"] = "未设置"
        return poll

    async def get_poll(self, poll_id: str) -> dict[str, Any] | None:
        async with self.lock:
            row = self.storage.get_poll(poll_id)
            return self._view(row) if row else None

    async def list_polls(
        self,
        platform_id: str,
        group: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        try:
            clean_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise PollError("Poll 列表 limit 必须是整数。") from exc
        if not 1 <= clean_limit <= 100:
            raise PollError("Poll 列表 limit 必须在 1 到 100 之间。")
        async with self.lock:
            group_id = None
            if str(group or "").strip():
                binding = self.storage.get_binding(str(group).strip(), platform_id)
                if binding is None:
                    raise PollError(f"未找到已绑定群：{group}")
                group_id = binding["group_id"]
            clean_status = str(status or "").strip().upper() or None
            if clean_status and clean_status not in {"OPEN", "CLOSED", "CANCELLED"}:
                raise PollError("Poll status 只能是 OPEN、CLOSED 或 CANCELLED。")
            return [self._view(row) for row in self.storage.list_polls(platform_id, group_id, clean_status, clean_limit)]

    async def list_open_polls(self, platform_id: str, group_id: str) -> list[dict[str, Any]]:
        async with self.lock:
            return [self._view(row) for row in self.storage.list_polls(platform_id, group_id, "OPEN", 100)]

    def _result_locked(self, poll_id: str) -> dict[str, Any]:
        options = self.storage.get_poll_options(poll_id)
        ballots = self.storage.list_poll_ballots(poll_id)
        counts = {int(option["option_id"]): 0 for option in options}
        for ballot in ballots:
            try:
                choices = json_loads_choices(ballot["choices_json"])
            except (TypeError, ValueError):
                choices = []
            for choice in choices:
                if choice in counts:
                    counts[choice] += 1
        participant_count = len(ballots)
        return {
            "participant_count": participant_count,
            "options": [
                {
                    "option_id": int(option["option_id"]),
                    "position": int(option["position"]),
                    "reply_key": option["reply_key"],
                    "label": option["label"],
                    "votes": counts[int(option["option_id"])],
                    "percentage": round(counts[int(option["option_id"])] * 100 / participant_count, 1) if participant_count else 0.0,
                }
                for option in options
            ],
        }

    async def get_result(self, poll_id: str) -> dict[str, Any]:
        async with self.lock:
            if self.storage.get_poll(poll_id) is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._result_locked(poll_id)

    async def cast_vote(
        self,
        poll_id: str,
        choices: list[int],
        voter_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not str(voter_id or "").strip():
            raise PollError("缺少投票成员 QQ。")
        if not isinstance(choices, list) or not choices:
            raise PollError("至少选择一个投票选项。")
        normalized: list[int] = []
        for choice in choices:
            if isinstance(choice, bool):
                raise PollError("投票选项编号无效。")
            try:
                normalized.append(int(choice))
            except (TypeError, ValueError) as exc:
                raise PollError("投票选项编号无效。") from exc
        if len(set(normalized)) != len(normalized):
            raise PollError("投票选项不能重复。")
        current = self._now(now)
        async with self.lock:
            poll = self.storage.get_poll(poll_id)
            if poll is None:
                raise PollError("投票不存在。")
            if poll["status"] != "OPEN":
                raise PollClosedError("投票已结束或已取消，不能投票。")
            deadline = poll.get("deadline_at")
            if deadline and parse_run_at_datetime(deadline, "UTC") <= current:
                self.storage.close_poll(poll_id, "deadline", current.isoformat(timespec="seconds"))
                raise PollClosedError("投票已截止，不能再投票。")
            option_ids = {int(row["option_id"]) for row in self.storage.get_poll_options(poll_id)}
            if any(choice not in option_ids for choice in normalized):
                raise PollError("包含不存在的投票选项。")
            if not poll["multiple_choice"] and len(normalized) != 1:
                raise PollError("单选投票只能选择一个选项。")
            if len(normalized) > int(poll["max_choices"]):
                raise PollError(f"最多选择 {poll['max_choices']} 个选项。")
            voter = str(voter_id).strip()
            existing = self.storage.get_poll_ballot(poll_id, voter)
            if existing and not poll["allow_change"]:
                raise PollError("该投票已提交，当前投票不可修改")
            timestamp = current.isoformat(timespec="seconds")
            self.storage.upsert_poll_ballot(
                poll_id, voter, normalized,
                existing["created_at"] if existing else timestamp,
                timestamp, bool(poll["allow_change"]),
            )
            return {"poll_id": poll_id, "choices": normalized, "changed": existing is not None}

    async def close_poll(self, poll_id: str, reason: str = "manual", now: datetime | None = None) -> dict[str, Any]:
        current = self._now(now)
        async with self.lock:
            poll = self.storage.get_poll(poll_id)
            if poll is None:
                raise PollError(f"投票不存在：{poll_id}")
            if poll["status"] == "CANCELLED":
                raise PollError("已取消的投票不能结束。")
            closed = self.storage.close_poll(poll_id, reason, current.isoformat(timespec="seconds"))
            return self._view(closed or poll)

    async def cancel_poll(self, poll_id: str, now: datetime | None = None) -> dict[str, Any]:
        current = self._now(now)
        async with self.lock:
            poll = self.storage.get_poll(poll_id)
            if poll is None:
                raise PollError(f"投票不存在：{poll_id}")
            if poll["status"] != "OPEN":
                raise PollError(f"投票当前不能取消：{poll['status']}")
            cancelled = self.storage.cancel_poll(poll_id, "cancelled", current.isoformat(timespec="seconds"))
            return self._view(cancelled or poll)

    async def close_due_polls(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = self._now(now)
        async with self.lock:
            due = self.storage.list_due_polls(current.isoformat(timespec="seconds"))
            closed: list[dict[str, Any]] = []
            for poll in due:
                row = self.storage.close_poll(poll["id"], "deadline", current.isoformat(timespec="seconds"))
                if row and row["status"] == "CLOSED":
                    closed.append(self._view(row))
            return closed

    async def pending_result_publications(self) -> list[dict[str, Any]]:
        async with self.lock:
            return [self._view(row) for row in self.storage.list_pending_poll_result_publications()]

    async def mark_announcement_sent(self, poll_id: str) -> dict[str, Any]:
        async with self.lock:
            row = self.storage.update_poll_flags(poll_id, self._now().isoformat(timespec="seconds"), announcement_sent=True)
            if row is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._view(row)

    async def mark_result_published(self, poll_id: str) -> dict[str, Any]:
        async with self.lock:
            row = self.storage.update_poll_flags(poll_id, self._now().isoformat(timespec="seconds"), result_published=True)
            if row is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._view(row)

    async def record_error(self, poll_id: str, error: str) -> dict[str, Any]:
        async with self.lock:
            row = self.storage.update_poll_flags(poll_id, self._now().isoformat(timespec="seconds"), last_error=str(error))
            if row is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._view(row)

    async def publishable_result_text(self, poll_id: str) -> str:
        async with self.lock:
            poll = self.storage.get_poll(poll_id)
            if poll is None:
                raise PollError(f"投票不存在：{poll_id}")
            result = self._result_locked(poll_id)
            lines = ["【投票结果】", "", poll["title"], ""]
            for option in result["options"]:
                lines.append(f"{option['position']}. {option['label']} — {option['votes']}票（{option['percentage']:.1f}%）")
            lines.extend(["", f"共{result['participant_count']}人参与", "投票已结束"])
            return "\n".join(lines)


def json_loads_choices(value: str) -> list[int]:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list):
        raise ValueError("choices_json 不是数组")
    return [int(item) for item in parsed]
