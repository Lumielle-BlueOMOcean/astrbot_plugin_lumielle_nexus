"""Persistent, anonymous web poll business logic for Lumielle Nexus."""

from __future__ import annotations

import hashlib
import json
import secrets
import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
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


class PollService:
    """Small async facade over Storage; the lock serializes vote transitions."""

    def __init__(
        self,
        storage: Storage,
        timezone_name: str = "Asia/Shanghai",
        web_enabled: bool = False,
        public_base_url: str = "",
    ) -> None:
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"无效时区：{timezone_name}") from exc
        self.timezone_name = timezone_name
        self.web_enabled = bool(web_enabled)
        self.public_base_url = str(public_base_url or "").strip().rstrip("/")
        self.storage = storage
        self.lock = asyncio.Lock()

    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(UTC)

    def _require_public_url(self) -> str:
        if not self.web_enabled:
            raise PollError("投票网页服务未开启，请先开启 poll_web_enabled。")
        if not self.public_base_url:
            raise PollError("未配置 poll_public_base_url，无法创建群投票。")
        parsed = urlparse(self.public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PollError("poll_public_base_url 必须是完整的 http/https 公网地址。")
        return self.public_base_url

    def public_url(self, token: str) -> str:
        return f"{self.public_base_url}/poll/{token}"

    @staticmethod
    def voter_hash(voter_token: str) -> str:
        token = str(voter_token or "")
        if not token:
            raise PollError("缺少投票浏览器 Cookie。")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _clean_options(options: list[str]) -> list[str]:
        if not isinstance(options, list) or not 2 <= len(options) <= 20:
            raise PollError("投票必须有 2 到 20 个选项。")
        cleaned: list[str] = []
        seen: set[str] = set()
        for option in options:
            label = str(option or "").strip()
            if not 1 <= len(label) <= 120:
                raise PollError("每个投票选项必须是 1 到 120 个字符。")
            key = label.casefold()
            if key in seen:
                raise PollError("投票选项不能重复。")
            seen.add(key)
            cleaned.append(label)
        return cleaned

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
        result_visibility: str = "after_close",
        auto_publish_result: bool = True,
        platform_id: str = "",
        creator_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        base_url = self._require_public_url()
        clean_title = str(title or "").strip()
        clean_description = str(description or "").strip()
        if not 1 <= len(clean_title) <= 200:
            raise PollError("投票标题必须是 1 到 200 个字符。")
        if len(clean_description) > 1000:
            raise PollError("投票说明最多 1000 个字符。")
        clean_options = self._clean_options(options)
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
            requested_max = len(clean_options)
        if not 1 <= requested_max <= len(clean_options):
            raise PollError("max_choices 必须在 1 到选项数之间。")
        visibility = str(result_visibility or "after_close").strip().casefold()
        if visibility not in {"live", "after_close"}:
            raise PollError("result_visibility 只能是 live 或 after_close。")
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
            token = secrets.token_urlsafe(32)
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
                "allow_change": bool(allow_change),
                "result_visibility": visibility,
                "auto_publish_result": bool(auto_publish_result),
                "deadline_at": deadline_at.isoformat(timespec="seconds") if deadline_at else None,
                "public_token": token,
                "created_at": created_at,
                "updated_at": created_at,
            }
            created = self.storage.create_poll(
                poll,
                [
                    {"option_id": index, "position": index, "label": label}
                    for index, label in enumerate(clean_options, start=1)
                ],
            )
            return self._view(created, base_url)

    def _view(self, row: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
        poll = dict(row)
        poll["multiple_choice"] = bool(poll.get("multiple_choice"))
        poll["allow_change"] = bool(poll.get("allow_change"))
        poll["auto_publish_result"] = bool(poll.get("auto_publish_result"))
        poll["announcement_sent"] = bool(poll.get("announcement_sent"))
        poll["result_published"] = bool(poll.get("result_published"))
        poll["options"] = self.storage.get_poll_options(poll["id"])
        poll["public_url"] = f"{(base_url or self.public_base_url).rstrip('/')}/poll/{poll['public_token']}"
        if poll.get("deadline_at"):
            deadline = datetime.fromisoformat(str(poll["deadline_at"]))
            poll["deadline_display"] = deadline.astimezone(self.timezone).strftime(
                "%Y-%m-%d %H:%M",
            )
        else:
            poll["deadline_display"] = "未设置"
        return poll

    async def get_poll(self, poll_id: str) -> dict[str, Any] | None:
        async with self.lock:
            row = self.storage.get_poll(poll_id)
            return self._view(row) if row else None

    async def get_poll_by_token(self, token: str) -> dict[str, Any] | None:
        async with self.lock:
            row = self.storage.get_poll_by_token(token)
            return self._view(row) if row else None

    async def get_ballot(self, token: str, voter_token: str) -> list[int]:
        voter_hash = self.voter_hash(voter_token)
        async with self.lock:
            poll = self.storage.get_poll_by_token(token)
            if poll is None:
                raise PollError("投票不存在。")
            ballot = self.storage.get_poll_ballot(poll["id"], voter_hash)
            if ballot is None:
                return []
            try:
                return json_loads_choices(ballot["choices_json"])
            except (TypeError, ValueError):
                return []

    async def list_polls(
        self,
        platform_id: str,
        group: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 100:
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
            return [self._view(row) for row in self.storage.list_polls(
                platform_id, group_id, clean_status, int(limit),
            )]

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
                    "label": option["label"],
                    "votes": counts[int(option["option_id"])],
                    "percentage": round(
                        counts[int(option["option_id"])] * 100 / participant_count,
                        1,
                    ) if participant_count else 0.0,
                }
                for option in options
            ],
        }

    async def get_result(self, poll_id: str) -> dict[str, Any]:
        async with self.lock:
            if self.storage.get_poll(poll_id) is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._result_locked(poll_id)

    async def get_public_result(self, token: str) -> dict[str, Any]:
        async with self.lock:
            poll = self.storage.get_poll_by_token(token)
            if poll is None:
                raise PollError("投票不存在。")
            visible = poll["status"] == "CLOSED" or (
                poll["status"] == "OPEN" and poll["result_visibility"] == "live"
            )
            response: dict[str, Any] = {
                "visible": visible,
                "status": poll["status"],
            }
            if visible:
                response["result"] = self._result_locked(poll["id"])
            else:
                response["message"] = "结果将在投票结束后公布。"
            return response

    async def vote(
        self,
        token: str,
        choices: list[int],
        voter_token: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        voter_hash = self.voter_hash(voter_token)
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
            poll = self.storage.get_poll_by_token(token)
            if poll is None:
                raise PollError("投票不存在。")
            if poll["status"] != "OPEN":
                raise PollClosedError("投票已结束或已取消，不能投票。")
            deadline = poll.get("deadline_at")
            if deadline and parse_run_at_datetime(deadline, "UTC") <= current:
                self.storage.close_poll(poll["id"], "deadline", current.isoformat(timespec="seconds"))
                raise PollClosedError("投票已截止，不能再投票。")
            option_ids = {int(row["option_id"]) for row in self.storage.get_poll_options(poll["id"])}
            if any(choice not in option_ids for choice in normalized):
                raise PollError("包含不存在的投票选项。")
            if not poll["multiple_choice"] and len(normalized) != 1:
                raise PollError("单选投票只能选择一个选项。")
            if len(normalized) > int(poll["max_choices"]):
                raise PollError(f"最多选择 {poll['max_choices']} 个选项。")
            existing = self.storage.get_poll_ballot(poll["id"], voter_hash)
            if existing and not poll["allow_change"]:
                raise PollError("该投票已提交，当前投票不可修改")
            self.storage.upsert_poll_ballot(
                poll["id"], voter_hash, normalized,
                existing["created_at"] if existing else current.isoformat(timespec="seconds"),
                current.isoformat(timespec="seconds"), bool(poll["allow_change"]),
            )
            return {
                "poll_id": poll["id"],
                "choices": normalized,
                "changed": existing is not None,
            }

    async def close_poll(
        self, poll_id: str, reason: str = "manual", now: datetime | None = None,
    ) -> dict[str, Any]:
        current = self._now(now)
        async with self.lock:
            poll = self.storage.get_poll(poll_id)
            if poll is None:
                raise PollError(f"投票不存在：{poll_id}")
            if poll["status"] == "CANCELLED":
                raise PollError("已取消的投票不能结束。")
            closed = self.storage.close_poll(poll_id, reason, current.isoformat(timespec="seconds"))
            return self._view(closed or poll)

    async def cancel_poll(
        self, poll_id: str, now: datetime | None = None,
    ) -> dict[str, Any]:
        current = self._now(now)
        async with self.lock:
            poll = self.storage.get_poll(poll_id)
            if poll is None:
                raise PollError(f"投票不存在：{poll_id}")
            if poll["status"] != "OPEN":
                raise PollError(f"投票当前不能取消：{poll['status']}")
            cancelled = self.storage.cancel_poll(
                poll_id, "cancelled", current.isoformat(timespec="seconds"),
            )
            return self._view(cancelled or poll)

    async def close_due_polls(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = self._now(now)
        async with self.lock:
            due = self.storage.list_due_polls(current.isoformat(timespec="seconds"))
            closed: list[dict[str, Any]] = []
            for poll in due:
                row = self.storage.close_poll(
                    poll["id"], "deadline", current.isoformat(timespec="seconds"),
                )
                if row and row["status"] == "CLOSED":
                    closed.append(self._view(row))
            return closed

    async def pending_result_publications(self) -> list[dict[str, Any]]:
        async with self.lock:
            return [
                self._view(row)
                for row in self.storage.list_pending_poll_result_publications()
            ]

    async def mark_announcement_sent(self, poll_id: str) -> dict[str, Any]:
        async with self.lock:
            row = self.storage.update_poll_flags(
                poll_id, self._now().isoformat(timespec="seconds"), announcement_sent=True,
            )
            if row is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._view(row)

    async def mark_result_published(self, poll_id: str) -> dict[str, Any]:
        async with self.lock:
            row = self.storage.update_poll_flags(
                poll_id, self._now().isoformat(timespec="seconds"), result_published=True,
            )
            if row is None:
                raise PollError(f"投票不存在：{poll_id}")
            return self._view(row)

    async def record_error(self, poll_id: str, error: str) -> dict[str, Any]:
        async with self.lock:
            row = self.storage.update_poll_flags(
                poll_id, self._now().isoformat(timespec="seconds"), last_error=str(error),
            )
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
                lines.append(
                    f"{option['option_id']}. {option['label']} — "
                    f"{option['votes']}票（{option['percentage']:.1f}%）"
                )
            lines.extend(["", f"共{result['participant_count']}人参与", "投票已结束"])
            return "\n".join(lines)


def json_loads_choices(value: str) -> list[int]:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list):
        raise ValueError("choices_json 不是数组")
    return [int(item) for item in parsed]
