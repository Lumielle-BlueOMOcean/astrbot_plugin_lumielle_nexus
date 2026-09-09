"""AstrBot entry point for 微光·群枢 / Lumielle Nexus."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools

if __package__:
    from .core import (
        MODERATION_CONFIRM_TTL_SECONDS,
        COLLECTION_CHECKPOINT_CHUNK_CHARS,
        COLLECTION_CHECKPOINT_MAX_CHUNKS,
        COLLECTION_CHECKPOINT_MAX_MESSAGES,
        COLLECTION_CHECKPOINT_MAX_TOTAL_CHARS,
        COLLECTION_CHECKPOINT_TIMEOUT_SECONDS,
        SUMMARY_PRIVATE_CHUNK_CHARS,
        TaskManager,
        clamp_archive_max_message_chars,
        clamp_archive_retention_days,
        clamp_scheduler_interval,
        collection_member_stats,
        deliver_mention_batches,
        deliver_text_chunks,
        build_collection_checkpoint_prompt,
        build_collection_checkpoint_system_prompt,
        extract_collection_reference_hints,
        format_local_time,
        generate_group_summary,
        member_display_name,
        member_role,
        next_interval_occurrence,
        is_group_admin_member,
        parse_collection_checkpoint_response,
        validate_collection_checkpoint_candidate,
        resolve_member_refs,
        search_group_members,
        validate_moderation_preflight,
    )
    from .exporter import export_collection
    from .qq_adapter import QQAdapter, QQAdapterError
    from .storage import Storage
else:
    from core import (
        MODERATION_CONFIRM_TTL_SECONDS,
        COLLECTION_CHECKPOINT_CHUNK_CHARS,
        COLLECTION_CHECKPOINT_MAX_CHUNKS,
        COLLECTION_CHECKPOINT_MAX_MESSAGES,
        COLLECTION_CHECKPOINT_MAX_TOTAL_CHARS,
        COLLECTION_CHECKPOINT_TIMEOUT_SECONDS,
        SUMMARY_PRIVATE_CHUNK_CHARS,
        TaskManager,
        clamp_archive_max_message_chars,
        clamp_archive_retention_days,
        clamp_scheduler_interval,
        collection_member_stats,
        deliver_mention_batches,
        deliver_text_chunks,
        build_collection_checkpoint_prompt,
        build_collection_checkpoint_system_prompt,
        extract_collection_reference_hints,
        format_local_time,
        generate_group_summary,
        member_display_name,
        member_role,
        next_interval_occurrence,
        is_group_admin_member,
        parse_collection_checkpoint_response,
        validate_collection_checkpoint_candidate,
        resolve_member_refs,
        search_group_members,
        validate_moderation_preflight,
    )
    from exporter import export_collection
    from qq_adapter import QQAdapter, QQAdapterError
    from storage import Storage

PLUGIN_NAME = "astrbot_plugin_lumielle_nexus"


class LumielleNexus(Star):
    def __init__(self, context: Context, config: Any) -> None:
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.storage = Storage(self.data_dir)
        self.manager = TaskManager(
            self.storage,
            timezone_name=str(self.config.get("timezone", "Asia/Shanghai")),
            max_retry_count=int(self.config.get("max_retry_count", 3)),
            archive_max_message_chars=clamp_archive_max_message_chars(
                self.config.get("archive_max_message_chars", 4000),
            ),
            archive_retention_days=clamp_archive_retention_days(
                self.config.get("archive_retention_days", 90),
            ),
        )
        operator_ids = self.config.get("operator_ids", []) or []
        self.operator_ids = {str(value).strip() for value in operator_ids if str(value).strip()}
        self.scheduler_interval_seconds = clamp_scheduler_interval(
            self.config.get("scheduler_interval_seconds", 15),
        )
        self.collection_ack = bool(self.config.get("collection_ack", True))
        self.moderation_enabled = bool(self.config.get("moderation_enabled", False))
        moderator_ids = self.config.get("moderator_ids", []) or []
        self.moderator_ids = {
            str(value).strip() for value in moderator_ids if str(value).strip()
        }
        self._scheduler_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(),
                name=f"{PLUGIN_NAME}-scheduler",
            )

    async def terminate(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        self.storage.close()

    def is_authorized_operator(self, event: AstrMessageEvent) -> bool:
        """Require a private message from an AstrBot admin or configured operator."""
        if not event.is_private_chat():
            return False
        if event.is_admin():
            return True
        return str(event.get_sender_id()).strip() in self.operator_ids

    @staticmethod
    def _event_platform_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        return str(getter() if callable(getter) else "")

    def _authorized_for_control(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if not event.is_private_chat():
            return False, "群内不接受群枢控制操作，请私聊机器人。"
        if self._event_platform_name(event) != "aiocqhttp":
            return False, "当前版本只支持通过 aiocqhttp（OneBot v11）控制。"
        if not self.is_authorized_operator(event):
            return False, "你没有群枢 operator 权限。"
        return True, ""

    def _moderator_denial(self, event: AstrMessageEvent) -> str:
        if not event.is_private_chat():
            return "群内不接受群管理操作，请私聊机器人。"
        if self._event_platform_name(event) != "aiocqhttp":
            return "当前版本只支持通过 aiocqhttp（OneBot v11）执行群管理。"
        if not self.moderation_enabled:
            return "群管理功能未开启。"
        if event.is_admin() or str(event.get_sender_id()).strip() in self.moderator_ids:
            return ""
        return "你没有 moderator 权限。"

    def _is_authorized_moderator(self, event: AstrMessageEvent) -> bool:
        return not self._moderator_denial(event)

    def _authorized_moderator(self, event: AstrMessageEvent) -> tuple[bool, str]:
        denial = self._moderator_denial(event)
        return not denial, denial

    async def _authorized_group_admin(
        self, event: AstrMessageEvent,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Authorize only a QQ-native owner/admin for the current bound group."""
        if event.is_private_chat():
            return False, "该权限只适用于当前 QQ 群消息。", None
        if self._event_platform_name(event) != "aiocqhttp":
            return False, "当前版本只支持 aiocqhttp（OneBot v11）群内控制。", None
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return False, "无法识别当前 QQ 群。", None
        try:
            binding = await self.manager.get_binding(group_id, self._platform_id(event))
            member = await self._adapter(event).get_group_member_info(
                group_id, str(event.get_sender_id()),
            )
        except (KeyError, ValueError, QQAdapterError) as exc:
            return False, f"当前群未绑定或无法读取群管理员身份：{exc}", None
        if not is_group_admin_member(member):
            return False, "你不是当前群的 QQ 群主/管理员，不能执行群枢控制。", None
        return True, "", binding

    async def _authorized_collection_control(
        self, event: AstrMessageEvent, requested_group: str = "",
    ) -> tuple[bool, str, str]:
        """Allow private operators or current-group QQ admins for scoped controls."""
        if event.is_private_chat():
            allowed, message = self._authorized_for_control(event)
            return allowed, message, str(requested_group or "")
        allowed, message, binding = await self._authorized_group_admin(event)
        if not allowed or binding is None:
            return False, message, ""
        requested = str(requested_group or "").strip()
        if requested and requested not in {binding["alias"], binding["group_id"]}:
            return False, "群内控制只能作用于当前群，不能跨群操作。", ""
        return True, "", binding["alias"]

    @staticmethod
    def _platform_id(event: AstrMessageEvent) -> str:
        return str(event.get_platform_id())

    def _adapter(self, event: AstrMessageEvent) -> QQAdapter:
        return QQAdapter(self.context, self._platform_id(event))

    @staticmethod
    def _payload(task: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(task.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _llm_response_text(response: Any) -> str:
        for attribute in ("completion_text", "text", "content"):
            value = getattr(response, attribute, None)
            if value:
                return str(value).strip()
        if isinstance(response, dict):
            for key in ("completion_text", "text", "content"):
                if response.get(key):
                    return str(response[key]).strip()
        return str(response).strip()

    @staticmethod
    def _event_source_message_id(event: AstrMessageEvent) -> str | None:
        for attribute in ("message_id", "get_message_id"):
            value = getattr(event, attribute, None)
            value = value() if callable(value) else value
            if value not in (None, ""):
                return str(value)
        for attribute in ("message_obj", "raw_message"):
            value = getattr(event, attribute, None)
            if isinstance(value, dict):
                for key in ("message_id", "id"):
                    if value.get(key) not in (None, ""):
                        return str(value[key])
            nested = getattr(value, "message_id", None)
            if nested not in (None, ""):
                return str(nested)
        return None

    @staticmethod
    def _command_args(event: AstrMessageEvent) -> list[str]:
        text = str(event.get_message_str() or "").strip()
        parts = text.split()
        return parts[2:] if len(parts) >= 2 else []

    def _task_line(self, task: dict[str, Any]) -> str:
        payload = self._payload(task)
        task_type = task["type"]
        if task_type == "DDL":
            label = f"{payload.get('title', 'DDL')}，截止 {format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')}"
        elif task_type == "RECURRING":
            label = f"{payload.get('message', '周期提醒')}，下次 {format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')}"
        elif task_type == "COURSE":
            label = f"{payload.get('course_name', '课程')}，下次提醒 {format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')}"
        elif task_type == "COLLECTION":
            label = payload.get("title") or task_type
        elif task_type == "SUMMARY":
            label = (
                f"每周{self._weekdays_text([payload.get('weekday', '')])} "
                f"{payload.get('time_of_day', '')}，下次 "
                f"{format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}"
            )
        elif task_type == "RELAY":
            label = f"待确认转述：{payload.get('content', '')[:80]}"
        elif task_type == "MODERATION":
            label = f"待确认群管理：{payload.get('action', '')} {payload.get('target_display_name', payload.get('target_id', ''))}"
        else:
            label = f"{payload.get('message', task_type)}，时间 {format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}"
        return f"{task['id']} [{task['status']}] {task['group_alias']}：{label}"

    @staticmethod
    def _weekdays_text(weekdays: list[int]) -> str:
        names = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
        return "、".join(names.get(int(day), str(day)) for day in weekdays)

    async def _task_details(self, event: AstrMessageEvent, task_id: str) -> str:
        if str(task_id).strip().upper().startswith("M-"):
            allowed, message = self._authorized_moderator(event)
            if not allowed:
                return message
        else:
            allowed, message, _effective_group = await self._authorized_collection_control(event)
            if not allowed:
                return message
        try:
            task = await self.manager.get_task(
                task_id,
                self._platform_id(event),
                str(event.get_sender_id()),
                allow_admin_override=event.is_admin(),
            )
            if not event.is_private_chat() and task["group_id"] != str(event.get_group_id()):
                return "群内控制只能作用于当前群，不能跨群操作。"
            payload = self._payload(task)
            lines = [f"任务：{task['id']}", f"类型：{task['type']}", f"群：{task['group_alias']}", f"状态：{task['status']}"]
            if task["type"] == "REMINDER":
                lines.extend([
                    f"时间：{format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}",
                    f"内容：{payload.get('message', '')}",
                ])
            elif task["type"] == "DDL":
                lines.extend([
                    f"标题：{payload.get('title', '')}",
                    f"截止：{format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}",
                    f"提前提醒（分钟）：{', '.join(str(item) for item in payload.get('remind_before_minutes', [])) or '无'}",
                ])
            elif task["type"] == "RECURRING":
                lines.extend([
                    f"星期：{self._weekdays_text(payload.get('weekdays', []))}",
                    f"时间：{payload.get('time_of_day', '')}",
                    f"下次：{format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}",
                ])
            elif task["type"] == "COURSE":
                lines.extend([
                    f"课程：{payload.get('course_name', '')}",
                    f"星期：{self._weekdays_text(payload.get('weekdays', []))}",
                    f"上课时间：{payload.get('start_time', '')}",
                    f"地点：{payload.get('location', '') or '未设置'}",
                    f"提前：{payload.get('remind_before_minutes', 0)} 分钟",
                    f"下次提醒：{format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}",
                    f"结束日期：{payload.get('end_date') or '未设置'}",
                ])
            elif task["type"] == "COLLECTION":
                status = await self.manager.collection_status(task["id"], self._platform_id(event))
                lines.extend([
                    f"标题：{payload.get('title', '')}",
                    f"提交人数：{status['submitted_count']}",
                    f"自然语言填写：{'开启' if payload.get('ai_extraction') else '关闭'}",
                ])
            elif task["type"] == "SUMMARY":
                lines.extend([
                    f"星期：{self._weekdays_text([payload.get('weekday', '')])}",
                    f"时间：{payload.get('time_of_day', '')}",
                    f"回看天数：{payload.get('lookback_days', 7)}",
                    f"focus：{payload.get('focus') or '未设置'}",
                    f"下次执行：{format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}",
                ])
            elif task["type"] == "RELAY":
                lines.extend([
                    f"@成员集合：{payload.get('mention_member_set')}（{len(payload.get('mention_user_ids') or [])} 人）"
                    if payload.get("mention_member_set")
                    else f"@全体：{'是' if payload.get('mention_all') else '否'}",
                    f"内容：{payload.get('content', '')}",
                    f"来源备注：{payload.get('source_note') or '无'}",
                ])
            elif task["type"] == "MODERATION":
                lines.extend([
                    f"动作：{payload.get('action', '')}",
                    f"目标：{payload.get('target_display_name', '')}（{payload.get('target_id', '')}）",
                    f"目标角色快照：{payload.get('target_role_snapshot', 'member')}",
                    f"Bot 角色快照：{payload.get('bot_role_snapshot', 'member')}",
                    f"时长：{payload.get('duration_seconds', 0)} 秒",
                    f"拒绝再次加群：{'是' if payload.get('reject_add_request') else '否'}",
                    f"原因：{payload.get('reason') or '未填写'}",
                ])
            return "\n".join(lines)
        except (KeyError, ValueError) as exc:
            return f"查询失败：{exc}"

    async def _bind_group(self, event: AstrMessageEvent, alias: str, group_id: str) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            info = await self._adapter(event).get_group_info(group_id)
            verified_id = str(info.get("group_id") or group_id)
            binding = await self.manager.bind_group(
                alias,
                verified_id,
                self._platform_id(event),
                event.get_sender_id(),
            )
            return f"已绑定群：{binding['alias']}（{binding['group_id']}）。"
        except (QQAdapterError, ValueError) as exc:
            return f"绑定失败：{exc}"

    async def _groups(self, event: AstrMessageEvent) -> str:
        groups = await self.manager.list_groups(self._platform_id(event))
        if not groups:
            return "还没有绑定群。示例：/nexus bind 班群 123456789"
        return "已绑定群：\n" + "\n".join(
            f"- {group['alias']}：{group['group_id']}" for group in groups
        )

    async def _live_members(
        self, event: AstrMessageEvent, group: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        binding = await self.manager.get_binding(group, self._platform_id(event))
        adapter = self._adapter(event)
        members = await adapter.get_group_member_list(binding["group_id"])
        login = await adapter.get_login_info()
        self_id = str(login.get("user_id") or event.get_self_id() or "")
        return binding, members, self_id

    async def _search_group_members(
        self, event: AstrMessageEvent, group: str, query: str = "", limit: int = 20,
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            _binding, members, self_id = await self._live_members(
                event, effective_group or group,
            )
            results = search_group_members(members, query, limit, self_id)
            if not results:
                return "没有找到匹配的群成员。"
            return "\n".join(
                f"{member.get('user_id')}\t{member_display_name(member)}\t{member_role(member)}"
                for member in results
            )
        except (KeyError, ValueError, QQAdapterError) as exc:
            return f"查询群成员失败：{exc}"

    async def _set_member_set(
        self,
        event: AstrMessageEvent,
        group: str,
        name: str,
        members: list[str],
        mode: str = "replace",
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            effective_group = effective_group or group
            _binding, live_members, self_id = await self._live_members(event, effective_group)
            member_set = await self.manager.set_member_set(
                effective_group, name, members, mode, self._platform_id(event),
                event.get_sender_id(), live_members, self_id,
            )
            return (
                f"已{('更新' if mode != 'replace' else '设置')}成员集合「{name.strip()}」："
                f"{len(member_set['members'])} 人。"
            )
        except (KeyError, ValueError, QQAdapterError) as exc:
            return f"设置成员集合失败：{exc}"

    async def _list_member_sets(self, event: AstrMessageEvent, group: str = "") -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            sets = await self.manager.list_member_sets(
                effective_group or group, self._platform_id(event),
            )
            if not sets:
                return "暂无成员集合。"
            aliases = {
                item["group_id"]: item["group_id"] for item in sets
            }
            for binding in await self.manager.list_groups(self._platform_id(event)):
                aliases[binding["group_id"]] = binding["alias"]
            lines = []
            current_group = None
            for item in sets:
                group_label = aliases.get(item["group_id"], item["group_id"])
                if group_label != current_group:
                    lines.append(f"{group_label}：")
                    current_group = group_label
                lines.append(f"- {item['name']}：{item['member_count']} 人")
            return "\n".join(lines)
        except (KeyError, ValueError) as exc:
            return f"查询成员集合失败：{exc}"

    async def _get_member_set(self, event: AstrMessageEvent, group: str, name: str) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            effective_group = effective_group or group
            _binding, live_members, self_id = await self._live_members(event, effective_group)
            member_set = await self.manager.get_member_set(
                effective_group, name, self._platform_id(event), live_members, self_id,
            )
            lines = [f"{member_set['name']}（{len(member_set['members'])} 人）"]
            for member in member_set["members"]:
                suffix = "" if member["present"] else " [已不在群]"
                lines.append(f"{member['user_id']} {member['display_name']}{suffix}")
            return "\n".join(lines)
        except (KeyError, ValueError, QQAdapterError) as exc:
            return f"查询成员集合失败：{exc}"

    async def _delete_member_set(
        self, event: AstrMessageEvent, group: str, name: str, confirm: bool = False,
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        if confirm is not True:
            return "这是永久删除操作，请明确确认删除该成员名单后再执行。"
        try:
            count = await self.manager.delete_member_set(
                effective_group or group, name, self._platform_id(event),
            )
            if not count:
                return f"未找到成员集合：{name}"
            return f"已删除成员集合「{name.strip()}」。"
        except (KeyError, ValueError) as exc:
            return f"删除成员集合失败：{exc}"

    async def _resolve_live_member(
        self, event: AstrMessageEvent, group: str, member: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        binding, live_members, self_id = await self._live_members(event, group)
        resolved = resolve_member_refs(live_members, [member], self_id)
        return binding, resolved[0], self_id

    async def _set_member_identity(
        self,
        event: AstrMessageEvent,
        group: str,
        member: str,
        name: str = "",
        student_id: str = "",
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            effective_group = effective_group or group
            _binding, target, _self_id = await self._resolve_live_member(
                event, effective_group, member,
            )
            source = "group_admin" if not event.is_private_chat() else "operator"
            rows = await self.manager.set_member_identity(
                effective_group,
                str(target["user_id"]),
                name,
                student_id,
                self._platform_id(event),
                str(event.get_sender_id()),
                source=source,
            )
            values = "、".join(f"{row['field_name']}：{row['value']}" for row in rows)
            return f"已保存 {member_display_name(target)}（{target['user_id']}）的身份信息：{values}。"
        except (KeyError, ValueError, QQAdapterError) as exc:
            return f"保存成员身份失败：{exc}"

    async def _get_member_identity(
        self, event: AstrMessageEvent, group: str, member: str,
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            effective_group = effective_group or group
            _binding, target, _self_id = await self._resolve_live_member(
                event, effective_group, member,
            )
            rows = await self.manager.get_member_identity(
                effective_group, str(target["user_id"]), self._platform_id(event),
            )
            if not rows:
                return f"{member_display_name(target)}（{target['user_id']}）暂无已保存的身份信息。"
            return "\n".join([
                f"成员：{member_display_name(target)}（{target['user_id']}）",
                *[
                    f"{row['field_name']}：{row['value']}（{'已核验' if row['verified'] else '未核验'}）"
                    for row in rows
                ],
            ])
        except (KeyError, ValueError, QQAdapterError) as exc:
            return f"查询成员身份失败：{exc}"

    async def _list_member_identities(
        self, event: AstrMessageEvent, group: str = "",
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            bindings = await self.manager.list_groups(self._platform_id(event))
            if effective_group or group:
                scopes = [(effective_group or group, "")]
            else:
                scopes = [(item["alias"], item["alias"]) for item in bindings]
            lines: list[str] = []
            for scope, label in scopes:
                rows = await self.manager.list_member_identities(
                    scope, self._platform_id(event),
                )
                if not rows:
                    continue
                if label:
                    lines.append(f"{label}：")
                grouped: dict[str, list[str]] = {}
                for row in rows:
                    grouped.setdefault(str(row["user_id"]), []).append(
                        f"{row['field_name']}={row['value']}"
                    )
                lines.extend(
                    f"{user_id}：{'；'.join(values)}"
                    for user_id, values in grouped.items()
                )
            return "\n".join(lines) if lines else "暂无已保存的成员身份信息。"
        except (KeyError, ValueError) as exc:
            return f"列出成员身份失败：{exc}"

    async def _set_archive(self, event: AstrMessageEvent, group: str, enabled: bool) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            await self.manager.set_archive(group, enabled, self._platform_id(event))
            if enabled:
                return f"已开启「{group}」消息归档。从现在开始记录新的文本消息，不会回溯开启前的历史。"
            return f"已关闭「{group}」消息归档。之后不再新增归档，已有数据不会自动删除。"
        except (KeyError, ValueError) as exc:
            return f"设置归档失败：{exc}"

    async def _archive_status(self, event: AstrMessageEvent, group: str) -> str:
        try:
            status = await self.manager.archive_status(group, self._platform_id(event))
            enabled = "开启" if status["enabled"] else "关闭"
            lines = [
                f"群：{status['alias']}",
                f"归档：{enabled}",
                f"已保存：{status['count']} 条文本消息",
                f"最早：{format_local_time(status['earliest'], self.manager.timezone_name) if status['earliest'] else '暂无'}",
                f"最新：{format_local_time(status['latest'], self.manager.timezone_name) if status['latest'] else '暂无'}",
            ]
            return "\n".join(lines)
        except (KeyError, ValueError) as exc:
            return f"查询归档失败：{exc}"

    async def _search_messages(
        self,
        event: AstrMessageEvent,
        group: str,
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
        limit: int = 50,
    ) -> str:
        try:
            rows = await self.manager.search_messages(
                group, keyword, start_time, end_time, limit, self._platform_id(event),
            )
            if not rows:
                return "没有找到符合条件的群消息。"
            return "\n".join(
                f"{format_local_time(row['sent_at'], self.manager.timezone_name, 'minutes')} "
                f"{row['sender_name']}：{row['message_text']}"
                for row in rows
            )
        except (KeyError, ValueError) as exc:
            return f"查询消息失败：{exc}"

    async def _summarize_group(
        self,
        event: AstrMessageEvent,
        group: str,
        start_time: str = "",
        end_time: str = "",
        focus: str = "",
        *,
        scheduled_window: tuple[str, str] | None = None,
        provider_id: str | None = None,
    ) -> str:
        try:
            snapshot = await self.manager.summary_snapshot(
                group,
                start_time if scheduled_window is None else scheduled_window[0],
                end_time if scheduled_window is None else scheduled_window[1],
                self._platform_id(event),
            )
            metadata = (
                "【群聊总结】\n"
                f"时间范围：{format_local_time(snapshot['window_start'], self.manager.timezone_name, 'minutes')}"
                f" 至 {format_local_time(snapshot['window_end'], self.manager.timezone_name, 'minutes')}\n"
                f"消息数：{snapshot['message_count']}\n"
                f"活跃成员数：{snapshot['unique_sender_count']}"
            )
            if snapshot["message_count"] == 0:
                return f"{metadata}\n\n该时间范围内没有归档文本消息。"
            if provider_id is None:
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin,
                )
            if not str(provider_id or "").strip():
                raise ValueError("当前会话没有可用的 LLM Provider")
            # generate_group_summary delegates the actual call to Context.llm_generate.
            summary = await generate_group_summary(
                self.context,
                str(provider_id),
                snapshot["messages"],
                snapshot["message_count"],
                snapshot["window_start"],
                snapshot["window_end"],
                focus,
            )
            if snapshot["truncated"]:
                metadata += (
                    "\n消息量较大，本次总结受输入预算限制，仅使用时间窗口内最近的部分文本消息。"
                    f"\n用于总结：{len(snapshot['messages'])} 条"
                )
            return f"{metadata}\n\n{summary}"
        except (KeyError, ValueError) as exc:
            return f"群消息已读取，但当前无法调用 AstrBot LLM Provider 生成总结：{exc}"
        except Exception as exc:
            logger.exception("群枢群聊总结失败")
            return f"群消息已读取，但当前无法调用 AstrBot LLM Provider 生成总结：{exc}"

    async def _create_weekly_summary(
        self,
        event: AstrMessageEvent,
        group: str,
        weekday: int,
        time_of_day: str,
        lookback_days: int = 7,
        focus: str = "",
    ) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin,
            )
            task = await self.manager.create_weekly_summary(
                group, weekday, time_of_day, lookback_days, focus, provider_id,
                event.get_sender_id(), event.unified_msg_origin,
                self._platform_id(event),
            )
            return (
                f"已创建每周群聊总结 {task['id']}，下次执行："
                f"{format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')}。"
                "届时会将总结私聊发给你。"
            )
        except (KeyError, ValueError) as exc:
            return f"创建周总结失败：{exc}"
        except Exception as exc:
            logger.exception("群枢周总结创建失败")
            return f"创建周总结失败：当前会话没有可用的 AstrBot LLM Provider（{exc}）"

    async def _prepare_relay(
        self,
        event: AstrMessageEvent,
        target_group: str,
        content: str,
        mention_all: bool = False,
        source_note: str = "",
        member_set: str = "",
    ) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        if mention_all and str(member_set or "").strip():
            return "准备转述失败：mention_all 与 member_set 互斥。"
        try:
            mention_user_ids: list[str] | None = None
            if str(member_set or "").strip():
                _binding, live_members, self_id = await self._live_members(
                    event, target_group,
                )
                snapshot = await self.manager.member_set_snapshot(
                    target_group,
                    member_set,
                    self._platform_id(event),
                    live_members,
                    self_id,
                )
                mention_user_ids = snapshot["user_ids"]
            task = await self.manager.prepare_relay(
                target_group, content, mention_all, source_note,
                self._platform_id(event), event.get_sender_id(), event.unified_msg_origin,
                member_set, mention_user_ids,
            )
            payload = self._payload(task)
            mention_line = (
                f"@成员集合：{payload.get('mention_member_set')}（{len(payload.get('mention_user_ids') or [])} 名当前成员）"
                if payload.get("mention_member_set")
                else f"@全体：{'是' if payload.get('mention_all') else '否'}"
            )
            return (
                f"待发送到：{task['group_alias']}\n"
                f"{mention_line}\n\n"
                f"内容：\n{payload.get('content', '')}\n\n"
                f"Relay ID：{task['id']}\n确认发送后我才会真正发到目标群。"
            )
        except (KeyError, ValueError) as exc:
            return f"准备转述失败：{exc}"

    async def _confirm_relay(self, event: AstrMessageEvent, task_id: str) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            task = await self.manager.confirm_relay(
                task_id,
                self._platform_id(event),
                str(event.get_sender_id()),
            )
            payload = self._payload(task)
            progress = self._payload({"payload": task.get("result")}).get(
                "mentioned_user_ids", [],
            )
            progress = [str(user_id).strip() for user_id in progress if str(user_id).strip()]
            try:
                adapter = self._adapter(event)
                if payload.get("mention_user_ids"):
                    await self.manager.update_reminder_result(task["id"], {
                        "mentioned_user_ids": progress,
                        "delivery_outcome": "pending",
                    })

                    async def send_batch(user_ids: list[str], text: str) -> Any:
                        return await adapter.send_group_at_member_batch(
                            task["group_id"], user_ids, text,
                        )

                    async def save_progress(user_ids: list[str]) -> Any:
                        progress[:] = user_ids
                        return await self.manager.update_reminder_result(task["id"], {
                            "mentioned_user_ids": list(progress),
                            "delivery_outcome": "pending",
                        })

                    await deliver_mention_batches(
                        payload["mention_user_ids"],
                        send_batch,
                        save_progress,
                        payload["content"],
                        progress,
                    )
                elif payload.get("mention_all"):
                    await adapter.send_group_at_all(task["group_id"], payload["content"])
                else:
                    await adapter.send_group_text(task["group_id"], payload["content"])
            except QQAdapterError as exc:
                all_ids = [
                    str(user_id).strip()
                    for user_id in payload.get("mention_user_ids", [])
                    if str(user_id).strip()
                ]
                mentioned = list(dict.fromkeys(progress))
                pending = [user_id for user_id in all_ids if user_id not in mentioned]
                outcome = "partial" if mentioned else "failed"
                await self.manager.finish_relay(
                    task["id"],
                    False,
                    str(exc),
                    {
                        "mentioned_user_ids": mentioned,
                        "pending_user_ids": pending,
                        "delivery_outcome": outcome,
                        "reason": "adapter_error",
                    },
                )
                if mentioned:
                    return (
                        f"Relay 部分发送成功：已完成 {len(mentioned)} 人，剩余 "
                        f"{len(pending)} 人未确认发送。该 Relay 不会自动重试，请检查群聊后重新准备需要补发的内容。"
                    )
                return f"转述发送失败：{exc}。该 Relay 已标记失败，请重新准备。"
            await self.manager.finish_relay(
                task["id"],
                True,
                result={
                    "mentioned_user_ids": list(dict.fromkeys(progress)),
                    "pending_user_ids": [],
                    "delivery_outcome": "completed",
                },
            )
            return f"已将 Relay {task['id']} 发送到「{task['group_alias']}」。"
        except (KeyError, ValueError) as exc:
            return f"确认转述失败：{exc}"

    async def _cancel_task(self, event: AstrMessageEvent, task_id: str) -> str:
        is_moderation = str(task_id).strip().upper().startswith("M-")
        if is_moderation:
            allowed, message = self._authorized_moderator(event)
        else:
            allowed, message, _effective_group = await self._authorized_collection_control(event)
        if not allowed:
            return message
        try:
            current_task = await self.manager.get_task(
                task_id,
                self._platform_id(event),
                str(event.get_sender_id()),
                allow_admin_override=event.is_admin(),
            )
            if not event.is_private_chat() and current_task["group_id"] != str(event.get_group_id()):
                return "群内控制只能作用于当前群，不能跨群操作。"
            task = await self.manager.cancel_task(
                task_id,
                self._platform_id(event),
                str(event.get_sender_id()),
                allow_admin_override=event.is_admin(),
            )
            return f"已取消任务：{task['id']}"
        except (KeyError, ValueError) as exc:
            return f"取消失败：{exc}"

    @staticmethod
    def _moderation_action_text(action: str) -> str:
        return {"mute": "禁言", "unmute": "解除禁言", "kick": "踢出群聊"}.get(
            str(action).casefold(), str(action),
        )

    async def _prepare_moderation(
        self,
        event: AstrMessageEvent,
        group: str,
        action: str,
        target: str,
        duration_seconds: int = 0,
        reject_add_request: bool = False,
        reason: str = "",
    ) -> str:
        allowed, message = self._authorized_moderator(event)
        if not allowed:
            return message
        try:
            binding, members, bot_id = await self._live_members(event, group)
            adapter = self._adapter(event)
            if str(target or "").strip() == bot_id:
                raise ValueError("不能对机器人自己执行群管理操作")
            target_member = resolve_member_refs(members, [target], bot_id)[0]
            bot_member = await adapter.get_group_member_info(binding["group_id"], bot_id)
            fresh_target = await adapter.get_group_member_info(
                binding["group_id"], target_member["user_id"],
            )
            target_member = {**target_member, **fresh_target}
            bot_member = {**bot_member, "user_id": bot_id}
            task = await self.manager.prepare_moderation(
                group,
                action,
                target_member["user_id"],
                member_display_name(target_member),
                member_role(target_member),
                member_role(bot_member),
                duration_seconds,
                reject_add_request,
                reason,
                self._platform_id(event),
                event.get_sender_id(),
                event.unified_msg_origin,
                bot_id=bot_id,
                target_member=target_member,
                bot_member=bot_member,
            )
            payload = self._payload(task)
            lines = [
                "【群管理操作预览】",
                f"群：{binding['alias']}",
                f"操作：{self._moderation_action_text(payload['action'])}",
                f"成员：{payload['target_display_name']}（{payload['target_id']}）",
                f"目标角色：{payload['target_role_snapshot']}",
            ]
            if payload["action"] == "mute":
                lines.append(f"时长：{payload['duration_seconds'] // 60} 分钟")
            elif payload["action"] == "unmute":
                lines.append("时长：0 秒")
            if payload["action"] == "kick":
                lines.extend([
                    "踢出群后该成员将离开群聊。",
                    f"拒绝再次加群申请：{'是' if payload['reject_add_request'] else '否'}",
                ])
            lines.extend([
                f"原因：{payload['reason'] or '未填写'}",
                f"Action ID：{task['id']}",
                f"预览有效期：{MODERATION_CONFIRM_TTL_SECONDS // 60} 分钟。",
                "确认执行后才会真正调用 QQ 群管理接口。",
            ])
            return "\n".join(lines)
        except (KeyError, ValueError, QQAdapterError) as exc:
            return f"准备群管理操作失败：{exc}"

    async def _confirm_moderation(self, event: AstrMessageEvent, task_id: str) -> str:
        allowed, message = self._authorized_moderator(event)
        if not allowed:
            return message
        try:
            task = await self.manager.confirm_moderation(
                task_id,
                self._platform_id(event),
                str(event.get_sender_id()),
            )
        except (KeyError, ValueError) as exc:
            return f"确认群管理失败：{exc}"

        payload = self._payload(task)
        adapter = self._adapter(event)
        try:
            login = await adapter.get_login_info()
            bot_id = str(login.get("user_id") or "")
            bot_member = await adapter.get_group_member_info(task["group_id"], bot_id)
            target_member = await adapter.get_group_member_info(
                task["group_id"], payload["target_id"],
            )
            bot_member = {**bot_member, "user_id": bot_id}
            preflight_error = validate_moderation_preflight(
                bot_member, target_member, bot_id, payload["target_id"],
            )
            if preflight_error:
                await self.manager.finish_moderation(
                    task["id"], False, "moderation_preflight_changed", {
                        "reason": "moderation_preflight_changed",
                    },
                )
                return f"确认群管理失败：{preflight_error}操作未发送。"
        except (QQAdapterError, KeyError, ValueError) as exc:
            await self.manager.finish_moderation(
                task["id"], False, "moderation_preflight_changed", {
                    "reason": "moderation_preflight_changed",
                },
            )
            return f"确认群管理失败：无法确认最新群成员权限（{exc}），操作未发送。"

        try:
            if payload["action"] in {"mute", "unmute"}:
                await adapter.set_group_ban(
                    task["group_id"],
                    payload["target_id"],
                    int(payload["duration_seconds"]),
                )
            else:
                await adapter.set_group_kick(
                    task["group_id"],
                    payload["target_id"],
                    bool(payload.get("reject_add_request")),
                )
            await self.manager.finish_moderation(
                task["id"], True, result={
                    "action": payload["action"],
                    "target_id": payload["target_id"],
                    "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )
            return f"已执行群管理操作：{self._moderation_action_text(payload['action'])} {payload['target_display_name']}。"
        except QQAdapterError as exc:
            await self.manager.finish_moderation(
                task["id"], False, str(exc), {"reason": "adapter_error"},
            )
            return f"群管理操作失败：{exc}。该操作不会自动重试。"
        except Exception as exc:
            logger.exception("群枢群管理执行失败 %s", task.get("id"))
            await self.manager.finish_moderation(
                task["id"], False, str(exc), {"reason": "execution_error"},
            )
            return f"群管理操作失败：{exc}。该操作不会自动重试。"

    async def _tasks(self, event: AstrMessageEvent, group: str | None = None) -> str:
        if event.is_private_chat():
            operator_allowed, operator_message = self._authorized_for_control(event)
            moderation_allowed, moderation_message = self._authorized_moderator(event)
            if not operator_allowed and not moderation_allowed:
                return operator_message or moderation_message
            effective_group = group or None
            moderation_only = moderation_allowed and not operator_allowed
        else:
            allowed, message, effective_group = await self._authorized_collection_control(
                event, group or "",
            )
            if not allowed:
                return message
            moderation_allowed = False
            moderation_only = False
        tasks = await self.manager.list_tasks(
            self._platform_id(event),
            effective_group,
            requester_id=str(event.get_sender_id()),
            allow_admin_override=event.is_admin(),
            moderation_only=moderation_only,
        )
        if not event.is_admin() and not moderation_allowed:
            tasks = [task for task in tasks if task["type"] != "MODERATION"]
        if not self.moderation_enabled:
            tasks = [task for task in tasks if task["type"] != "MODERATION"]
        if not tasks:
            return "暂无任务。"
        return "任务列表：\n" + "\n".join(self._task_line(task) for task in tasks[:30])

    async def _start_collection(
        self,
        event: AstrMessageEvent,
        group: str,
        title: str,
        fields: list[str],
        announcement: str = "",
        mention_all: bool = False,
        target_member_set: str = "",
        ai_extraction: bool = False,
        chase_at: str = "",
        deadline: str = "",
        missing_default_field: str = "",
        missing_default_value: str = "",
        auto_export: bool = False,
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        try:
            if not isinstance(ai_extraction, bool):
                return "创建收集任务失败：ai_extraction 必须是布尔值。"
            ai_provider_id = ""
            if ai_extraction:
                try:
                    ai_provider_id = str(
                        await self.context.get_current_chat_provider_id(
                            event.unified_msg_origin,
                        ) or "",
                    ).strip()
                except Exception:
                    logger.exception("群枢自然语言收集 Provider 获取失败")
                    return (
                        "当前控制会话没有可用的 AstrBot LLM Provider，"
                        "无法开启自然语言填写。可以关闭 ai_extraction 后使用标准字段格式提交。"
                    )
                if not ai_provider_id:
                    return (
                        "当前控制会话没有可用的 AstrBot LLM Provider，"
                        "无法开启自然语言填写。可以关闭 ai_extraction 后使用标准字段格式提交。"
                    )
            task = await self.manager.start_collection(
                effective_group or group,
                title,
                fields,
                announcement,
                mention_all,
                self._platform_id(event),
                event.get_sender_id(),
                event.unified_msg_origin,
                target_member_set=target_member_set,
                ai_extraction=ai_extraction,
                ai_provider_id=ai_provider_id,
                chase_at=chase_at,
                deadline=deadline,
                missing_default_field=missing_default_field,
                missing_default_value=missing_default_value,
                auto_export=auto_export,
            )
            payload = self._payload(task)
            notice = payload.get("announcement") or (
                f"【{payload['title']}】\n\n请提交以下信息：\n\n"
                + "\n".join(f"{field}：" for field in payload["fields"])
                + "\n\n直接在群内按以上格式发送即可。"
            )
            if payload.get("ai_extraction"):
                notice += (
                    "\n\n请在截止前直接用自然语言回复自己的情况；"
                    "checkpoint 会批量分析新增消息。\n"
                    "例如：我7号下午三点左右回来。\n"
                    "标准“字段：值”格式仍然最可靠，会立即记录。"
                )
            if payload.get("chase_at"):
                notice += f"\n计划催办时间：{format_local_time(payload['chase_at'], self.manager.timezone_name, 'minutes')}。"
            if payload.get("deadline"):
                notice += f"\n截止时间：{format_local_time(payload['deadline'], self.manager.timezone_name, 'minutes')}。"
            try:
                if payload.get("mention_all"):
                    await self._adapter(event).send_group_at_all(task["group_id"], notice)
                else:
                    await self._adapter(event).send_group_text(task["group_id"], notice)
            except QQAdapterError as exc:
                await self.manager.fail_collection(task["id"], f"群启动通知发送失败：{exc}")
                return f"创建统计失败：群启动通知发送失败：{exc}"
            return f"已开始收集：{task['id']}（{task['group_alias']}）。"
        except (KeyError, ValueError) as exc:
            return f"创建收集任务失败：{exc}"

    async def _collection_status(
        self,
        event: AstrMessageEvent,
        task_id: str = "",
        group: str = "",
        refresh: bool = True,
    ) -> str:
        allowed, message, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return message
        if not isinstance(refresh, bool):
            return "查询失败：refresh 必须是布尔值。"
        try:
            hints = {"task_ids": [], "aliases": []}
            if not str(task_id or "").strip() and not str(group or "").strip() and event.is_private_chat():
                hints = await self._conversation_hints(event)
            current_group_id = str(event.get_group_id() or "") if not event.is_private_chat() else ""
            task = await self.manager.resolve_collection_reference(
                self._platform_id(event),
                task_id,
                effective_group or group,
                hinted_task_ids=hints["task_ids"],
                hinted_aliases=hints["aliases"],
                current_group_id=current_group_id,
            )
            if not event.is_private_chat() and task["group_id"] != current_group_id:
                return "群内控制只能作用于当前群，不能跨群操作。"
            refresh_error = ""
            payload = self._payload(task)
            if refresh and task["status"] == "ACTIVE" and payload.get("ai_extraction"):
                try:
                    await self._run_collection_checkpoint(task["id"], datetime.now(timezone.utc), "manual")
                    task = await self.manager.get_task(task["id"], self._platform_id(event))
                except Exception as exc:
                    refresh_error = str(exc)
                    logger.exception("群枢 Collection 状态 refresh 失败 %s", task["id"])
            status = await self.manager.collection_status(task["id"], self._platform_id(event))
            task = status["task"]
            members: list[dict[str, Any]] | None = None
            member_error = ""
            try:
                members = await self._adapter(event).get_group_member_list(task["group_id"])
            except QQAdapterError as exc:
                member_error = str(exc)
            if members is not None:
                target_ids = self._payload(task).get("target_member_ids")
                member_stats = collection_member_stats(
                    members,
                    status["entries"],
                    self_id=str(event.get_self_id()),
                    target_ids=target_ids,
                )
                submitted = len(member_stats["submitted_ids"])
            else:
                member_stats = None
                submitted = status["submitted_count"]
            lines = [
                f"{task['id']}：{self._payload(task).get('title', task['group_alias'])}",
                f"状态：{task['status']}",
                f"已提交：{submitted} 人",
                f"自然语言填写：{'开启' if self._payload(task).get('ai_extraction') else '关闭'}",
            ]
            if refresh_error:
                lines.append(f"增量分析未完成：{refresh_error}；cursor 未推进。")
            if members is not None:
                lines.extend([
                    f"群成员：{len(member_stats['eligible_ids'])} 人",
                    f"未提交：{len(member_stats['missing_ids'])} 人",
                ])
            else:
                lines.append(f"群成员数：暂时无法获取（{member_error}）")
            return "\n".join(lines)
        except (KeyError, ValueError) as exc:
            return f"查询失败：{exc}"

    async def _stop_collection(self, event: AstrMessageEvent, task_id: str) -> str:
        allowed, message, _effective_group = await self._authorized_collection_control(event)
        if not allowed:
            return message
        try:
            current_task = await self.manager.get_task(
                task_id, self._platform_id(event),
            )
            if not event.is_private_chat() and current_task["group_id"] != str(event.get_group_id()):
                return "群内控制只能作用于当前群，不能跨群操作。"
            snapshot = await self.manager.stop_collection(task_id, self._platform_id(event))
            task = snapshot["task"]
            target_ids = self._payload(task).get("target_member_ids")
            members: list[dict[str, Any]] | None = None
            member_error = ""
            try:
                members = await self._adapter(event).get_group_member_list(task["group_id"])
            except QQAdapterError as exc:
                member_error = str(exc)
            export_task = dict(task)
            export_task["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            identities = await self.manager.list_member_identities(
                task["group_alias"], self._platform_id(event),
            )
            try:
                output = export_collection(
                    self.data_dir / "exports",
                    export_task,
                    snapshot["entries"],
                    members,
                    self_id=str(event.get_self_id()),
                    target_ids=target_ids,
                    timezone_name=self.manager.timezone_name,
                    identities=identities,
                )
            except Exception as exc:
                await self.manager.fail_collection(task["id"], f"Excel 导出失败：{exc}")
                return f"统计已停止，但 Excel 导出失败：{exc}"

            upload_error = ""
            try:
                await self._adapter(event).upload_private_file(task["creator_id"], output)
            except QQAdapterError as exc:
                upload_error = str(exc)
            result = {
                "export_path": str(output),
                "submitted_count": len(snapshot["entries"]),
                "member_count": (
                    len(collection_member_stats(
                        members,
                        snapshot["entries"],
                        self_id=str(event.get_self_id()),
                        target_ids=target_ids,
                    )["eligible_ids"])
                    if members is not None else None
                ),
                "member_error": member_error or None,
                "upload_error": upload_error or None,
            }
            await self.manager.complete_collection(task["id"], result)
            summary = f"统计已结束：{task['id']}，提交 {len(snapshot['entries'])} 人。Excel 已生成：{output.name}"
            if upload_error:
                summary += f"\n但 QQ 文件回传失败：{upload_error}"
            return summary
        except (KeyError, ValueError) as exc:
            return f"结束失败：{exc}"

    async def _conversation_hints(self, event: AstrMessageEvent) -> dict[str, list[str]]:
        conversation_manager = getattr(self.context, "conversation_manager", None)
        if conversation_manager is None:
            return {"task_ids": [], "aliases": []}
        try:
            conversations = await conversation_manager.get_conversations(
                unified_msg_origin=event.unified_msg_origin,
                platform_id=self._platform_id(event),
            )
        except Exception:
            logger.exception("群枢读取 ConversationManager 线索失败")
            return {"task_ids": [], "aliases": []}
        parts: list[str] = []
        total = 0
        for conversation in list(conversations or [])[:5]:
            title = getattr(conversation, "title", "")
            history = getattr(conversation, "history", "")
            if isinstance(conversation, dict):
                title = conversation.get("title", "")
                history = conversation.get("history", conversation.get("content", ""))
            if not isinstance(history, str):
                history = json.dumps(history, ensure_ascii=False)
            text = f"{title}\n{history}"
            remaining = 20000 - total
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            total += min(len(text), remaining)
        try:
            groups = await self.manager.list_groups(self._platform_id(event))
            aliases = [str(item["alias"]) for item in groups]
        except Exception:
            aliases = []
        return extract_collection_reference_hints("\n".join(parts), aliases)

    @staticmethod
    def _checkpoint_member_records(
        snapshot: dict[str, Any], rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rows = snapshot.get("messages") if rows is None else rows
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            grouped.setdefault(str(row["sender_id"]), []).append(row)
        records: list[dict[str, Any]] = []
        for user_id, user_rows in grouped.items():
            identity = {
                str(item["field_name"]): str(item["value"])
                for item in snapshot.get("identities", {}).get(user_id, [])
            }
            records.append({
                "user_id": user_id,
                "identity": identity,
                "current_entry": snapshot.get("current_entries", {}).get(user_id, {}),
                "messages": [
                    {
                        "sent_at": row["sent_at"],
                        "message_text": row["message_text"],
                    }
                    for row in user_rows
                ],
            })
        return records

    @staticmethod
    def _checkpoint_chunks(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for row in rows:
            row_chars = len(str(row.get("message_text") or "")) + 32
            if current and current_chars + row_chars > COLLECTION_CHECKPOINT_CHUNK_CHARS:
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(row)
            current_chars += row_chars
        if current:
            chunks.append(current)
        if len(chunks) <= COLLECTION_CHECKPOINT_MAX_CHUNKS:
            return chunks
        return chunks[:COLLECTION_CHECKPOINT_MAX_CHUNKS]

    async def _call_collection_checkpoint_llm(
        self,
        snapshot: dict[str, Any],
        members: list[dict[str, Any]],
        *,
        notes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = snapshot["payload"]
        provider_id = str(payload.get("ai_provider_id") or "").strip()
        if not provider_id:
            raise ValueError("Collection 没有可用的固定 LLM Provider")
        prompt = build_collection_checkpoint_prompt(
            payload.get("title", ""),
            payload.get("fields", []),
            payload.get("announcement", ""),
            snapshot["cutoff"],
            self.manager.timezone_name,
            members,
            notes=notes,
        )
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=build_collection_checkpoint_system_prompt(),
            ),
            timeout=COLLECTION_CHECKPOINT_TIMEOUT_SECONDS,
        )
        return parse_collection_checkpoint_response(self._llm_response_text(response))

    async def _generate_collection_checkpoint_candidate(
        self, snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        rows = snapshot.get("messages") or []
        if not rows:
            return {"members": []}
        chunks = self._checkpoint_chunks(rows)
        if len(chunks) == 1:
            return await self._call_collection_checkpoint_llm(
                snapshot, self._checkpoint_member_records(snapshot, chunks[0]),
            )
        notes: list[dict[str, Any]] = []
        for chunk in chunks:
            notes.append(await self._call_collection_checkpoint_llm(
                snapshot, self._checkpoint_member_records(snapshot, chunk),
            ))
        if len(notes) >= 3:
            return self._merge_checkpoint_candidates(notes)
        return await self._call_collection_checkpoint_llm(
            snapshot,
            self._checkpoint_member_records(snapshot, rows),
            notes=notes,
        )

    @staticmethod
    def _merge_checkpoint_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        by_user: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            for member in candidate.get("members", []) if isinstance(candidate, dict) else []:
                if not isinstance(member, dict):
                    continue
                user_id = str(member.get("user_id") or "").strip()
                if not user_id:
                    continue
                target = by_user.setdefault(user_id, {"user_id": user_id, "status": "ok", "items": []})
                items_by_field = {str(item.get("field")): item for item in target["items"]}
                for item in member.get("items", []) if isinstance(member.get("items"), list) else []:
                    if isinstance(item, dict):
                        items_by_field[str(item.get("field"))] = item
                target["items"] = list(items_by_field.values())
        return {"members": list(by_user.values())}

    async def _run_collection_checkpoint(
        self,
        task_id: str,
        cutoff: str | datetime,
        reason: str,
    ) -> dict[str, Any]:
        snapshot = await self.manager.prepare_collection_checkpoint(task_id, cutoff, reason)
        if not snapshot["messages"] or not snapshot["payload"].get("ai_extraction"):
            await self.manager.advance_collection_checkpoint(task_id, snapshot)
            return {"snapshot": snapshot, "llm_called": False, "applied": []}
        candidate = await self._generate_collection_checkpoint_candidate(snapshot)
        applied = await self.manager.apply_collection_checkpoint(task_id, snapshot, candidate)
        return {"snapshot": snapshot, "llm_called": True, **applied}

    async def _execute_collection_checkpoint(
        self, task: dict[str, Any], payload: dict[str, Any],
    ) -> None:
        status = await self.manager.collection_status(
            payload["collection_task_id"], task["platform_id"],
        )
        if status["task"]["status"] != "ACTIVE":
            return
        await self._run_collection_checkpoint(
            payload["collection_task_id"],
            payload.get("scheduled_run_at") or task["run_at"],
            payload.get("checkpoint_type", "chase"),
        )
        if payload.get("checkpoint_type") == "chase":
            collection = (await self.manager.collection_status(
                payload["collection_task_id"], task["platform_id"],
            ))["task"]
            collection_payload = self._payload(collection)
            if collection_payload.get("deadline"):
                deadline = datetime.fromisoformat(collection_payload["deadline"])
                if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
                    return
            await self._execute_collection_chase(task, payload)

    async def _execute_collection_finalize(
        self, task: dict[str, Any], payload: dict[str, Any],
    ) -> None:
        status = await self.manager.collection_status(
            payload["collection_task_id"], task["platform_id"],
        )
        collection = status["task"]
        if collection["status"] != "ACTIVE":
            return
        collection_payload = self._payload(collection)
        analysis_incomplete = False
        analysis_error = ""
        try:
            await self._run_collection_checkpoint(
                collection["id"],
                collection_payload.get("deadline") or payload.get("scheduled_run_at") or task["run_at"],
                "finalize",
            )
        except Exception as exc:
            analysis_incomplete = True
            analysis_error = str(exc)
            logger.exception("群枢 Collection 最终 checkpoint 失败 %s", collection["id"])

        adapter = QQAdapter(self.context, task["platform_id"])
        members: list[dict[str, Any]] | None = None
        self_id: str | None = None
        try:
            members = await adapter.get_group_member_list(collection["group_id"])
            self_id = str((await adapter.get_login_info()).get("user_id") or "")
        except QQAdapterError:
            logger.exception("群枢 Collection 最终群成员读取失败 %s", collection["id"])
        if not analysis_incomplete and collection_payload.get("missing_default_field") and members is not None:
            latest_status = await self.manager.collection_status(
                collection["id"], task["platform_id"],
            )
            eligible = collection_member_stats(
                members,
                latest_status["entries"],
                self_id=self_id,
                target_ids=collection_payload.get("target_member_ids"),
            )["eligible_ids"]
            await self.manager.apply_missing_default(
                collection["id"], eligible,
                collection_payload["missing_default_field"],
                collection_payload["missing_default_value"],
            )
        snapshot = await self.manager.stop_collection(collection["id"], task["platform_id"])
        export_path = None
        upload_error = ""
        if collection_payload.get("auto_export"):
            export_task = dict(snapshot["task"])
            export_task["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                identities = await self.manager.list_member_identities(
                    collection["group_alias"], task["platform_id"],
                )
                export_path = export_collection(
                    self.data_dir / "exports", export_task, snapshot["entries"], members,
                    self_id=self_id, target_ids=collection_payload.get("target_member_ids"),
                    timezone_name=self.manager.timezone_name, identities=identities,
                )
                try:
                    await adapter.upload_private_file(collection["creator_id"], export_path)
                except QQAdapterError as exc:
                    upload_error = str(exc)
            except Exception as exc:
                analysis_error = analysis_error or f"Excel 导出失败：{exc}"
                logger.exception("群枢 Collection 自动导出失败 %s", collection["id"])
        result = {
            "submitted_count": len(snapshot["entries"]),
            "auto_export": bool(collection_payload.get("auto_export")),
            "export_path": str(export_path) if export_path else None,
            "upload_error": upload_error or None,
            "analysis_incomplete": analysis_incomplete,
            "analysis_error": analysis_error or None,
        }
        await self.manager.complete_collection(collection["id"], result)

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.manager.prune_archive_if_due()
                await self.manager.materialize_due_schedules()
                for task in await self.manager.due_tasks():
                    try:
                        executed = await self._execute_reminder(task)
                        if executed:
                            await self.manager.finish_reminder(task["id"], True)
                    except Exception as exc:
                        logger.exception("群枢提醒任务执行失败 %s", task.get("id"))
                        try:
                            await self.manager.finish_reminder(task["id"], False, str(exc))
                        except Exception:
                            logger.exception("群枢提醒任务状态更新失败 %s", task.get("id"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("群枢 scheduler 迭代失败")
            await asyncio.sleep(self.scheduler_interval_seconds)

    async def _execute_reminder(self, task: dict[str, Any]) -> bool:
        skip_reason = await self.manager.reminder_skip_reason(task)
        if skip_reason:
            await self.manager.skip_reminder(
                task["id"],
                skip_reason,
                status="CANCELLED" if skip_reason == "parent_cancelled" else "COMPLETED",
            )
            return False
        payload = self._payload(task)
        if payload.get("kind") == "collection_checkpoint":
            await self._execute_collection_checkpoint(task, payload)
            return True
        if payload.get("kind") == "collection_finalize":
            await self._execute_collection_finalize(task, payload)
            return True
        if payload.get("kind") == "collection_chase":
            await self._execute_collection_chase(task, payload)
            return True
        if payload.get("kind") == "weekly_summary":
            await self._execute_weekly_summary(task, payload)
            return True
        adapter = QQAdapter(self.context, task["platform_id"])
        if payload.get("mention_all"):
            await adapter.send_group_at_all(task["group_id"], payload["message"])
        else:
            await adapter.send_group_text(task["group_id"], payload["message"])
        return True

    async def _execute_weekly_summary(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        cached = self._payload({"payload": task.get("result")})
        summary_text = cached.get("summary_text")
        if not summary_text:
            scheduled = datetime.fromisoformat(payload["scheduled_run_at"])
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            window_end = scheduled.astimezone(timezone.utc)
            window_start = window_end - timedelta(days=int(payload["lookback_days"]))
            snapshot = await self.manager.summary_snapshot(
                task["group_id"],
                window_start.isoformat(timespec="seconds"),
                window_end.isoformat(timespec="seconds"),
                task["platform_id"],
            )
            summary_body = await generate_group_summary(
                self.context,
                str(payload["provider_id"]),
                snapshot["messages"],
                snapshot["message_count"],
                snapshot["window_start"],
                snapshot["window_end"],
                str(payload.get("focus") or ""),
            )
            summary_text = (
                "【群聊周报】\n"
                f"时间范围：{format_local_time(snapshot['window_start'], self.manager.timezone_name, 'minutes')}"
                f" 至 {format_local_time(snapshot['window_end'], self.manager.timezone_name, 'minutes')}\n"
                f"消息数：{snapshot['message_count']}\n"
                f"活跃成员数：{snapshot['unique_sender_count']}"
                + (
                    "\n消息量较大，本次总结受输入预算限制，仅使用时间窗口内最近的部分文本消息。"
                    f"\n用于总结：{len(snapshot['messages'])} 条"
                    if snapshot["truncated"] else ""
                )
                + f"\n\n{summary_body}"
            )
            cached = {
                "summary_text": summary_text,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "window_start": snapshot["window_start"],
                "window_end": snapshot["window_end"],
                "sent_chunk_count": 0,
            }
            await self.manager.update_reminder_result(task["id"], cached)
        cached["summary_text"] = summary_text
        adapter = QQAdapter(self.context, task["platform_id"])

        async def send_chunk(chunk: str) -> Any:
            return await adapter.send_private_message(task["creator_id"], chunk)

        async def save_progress(sent_chunk_count: int) -> None:
            cached["sent_chunk_count"] = sent_chunk_count
            await self.manager.update_reminder_result(task["id"], cached)

        await deliver_text_chunks(
            summary_text,
            SUMMARY_PRIVATE_CHUNK_CHARS,
            int(cached.get("sent_chunk_count") or 0),
            send_chunk,
            save_progress,
        )

    async def _execute_collection_chase(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        status = await self.manager.collection_status(
            payload["collection_task_id"], task["platform_id"],
        )
        collection = status["task"]
        if collection["status"] != "ACTIVE":
            return
        collection_payload = self._payload(collection)
        if collection_payload.get("deadline"):
            deadline = datetime.fromisoformat(collection_payload["deadline"])
            if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
                return
        adapter = QQAdapter(self.context, task["platform_id"])
        members = await adapter.get_group_member_list(task["group_id"])
        try:
            self_id = str((await adapter.get_login_info()).get("user_id") or "")
        except QQAdapterError:
            self_id = None
        stats = collection_member_stats(
            members,
            status["entries"],
            self_id=self_id,
            target_ids=collection_payload.get("target_member_ids"),
        )
        missing_set = stats["missing_ids"]
        missing_ids: list[str] = []
        seen_missing: set[str] = set()
        for member in members:
            member_id = str(member.get("user_id") or "").strip()
            if member_id in missing_set and member_id not in seen_missing:
                missing_ids.append(member_id)
                seen_missing.add(member_id)
        if not missing_ids:
            return
        stored_result = self._payload({"payload": task.get("result")})
        mentioned_ids = [
            str(user_id).strip()
            for user_id in stored_result.get("mentioned_user_ids", [])
            if str(user_id).strip()
        ]
        await self.manager.update_reminder_result(task["id"], {
            "mentioned_user_ids": list(dict.fromkeys(mentioned_ids)),
        })

        async def send_batch(user_ids: list[str], text: str) -> Any:
            return await adapter.send_group_at_member_batch(
                task["group_id"], user_ids, text,
            )

        async def save_progress(user_ids: list[str]) -> Any:
            mentioned_ids[:] = user_ids
            return await self.manager.update_reminder_result(task["id"], {
                "mentioned_user_ids": list(mentioned_ids),
            })

        # The adapter's send_group_at_members convenience API remains available,
        # but chase delivery uses the single-batch primitive for durable progress.
        mentioned_set = set(mentioned_ids)
        remaining_ids = [
            user_id for user_id in missing_ids if user_id not in mentioned_set
        ]
        if remaining_ids:
            await deliver_mention_batches(
                remaining_ids,
                send_batch,
                save_progress,
                payload["message"],
                mentioned_ids,
            )
        interval = int(payload.get("repeat_interval_minutes") or 0)
        if interval:
            try:
                scheduled_run_at = payload.get("scheduled_run_at") or task["run_at"]
                next_run = next_interval_occurrence(
                    scheduled_run_at,
                    interval,
                    datetime.now(timezone.utc),
                )
                await self.manager.schedule_collection_chase(
                    collection["id"],
                    next_run,
                    payload["message"],
                    interval,
                    task["platform_id"],
                    task["creator_id"],
                    task["creator_private_origin"],
                )
            except (KeyError, ValueError):
                logger.exception("群枢重复催办未能安排下一次 %s", task.get("id"))

    @filter.command_group("nexus")
    def nexus(self) -> None:
        """微光·群枢 command group."""

    @nexus.command("help", priority=10)
    async def cmd_help(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            yield event.plain_result(message)
            return
        yield event.plain_result(
            "群枢命令：\n"
            "/nexus bind <别名> <群号>\n"
            "/nexus groups\n"
            "/nexus tasks [群别名]\n"
            "/nexus task <任务ID>\n"
            "/nexus cancel <任务ID>（提醒或可取消的时间任务父任务）\n"
            "/nexus archive <群别名> on|off\n"
            "/nexus archive-status <群别名>\n"
            "/nexus relay-confirm <X-任务ID>\n"
            "/nexus members <群别名> [关键词]\n"
            "/nexus member-sets [群别名]\n"
            "/nexus member-set <群别名> <集合名>\n"
            "/nexus moderation-confirm <M-任务ID>\n"
            "/nexus collect-start 群别名|标题|字段1,字段2[,公告][,all]\n"
            "/nexus collect-status <任务ID>\n"
            "/nexus collect-stop <任务ID>\n"
            "也可以直接私聊使用自然语言。",
        )

    @nexus.command("bind", priority=10)
    async def cmd_bind(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._bind_group(event, args[0], args[1])
            if len(args) >= 2
            else "用法：/nexus bind <别名> <群号>",
        )

    @nexus.command("groups", priority=10)
    async def cmd_groups(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        yield event.plain_result(message if not allowed else await self._groups(event))

    @nexus.command("archive", priority=10)
    async def cmd_archive(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        if len(args) < 2 or args[1].casefold() not in {"on", "off", "开启", "关闭"}:
            yield event.plain_result("用法：/nexus archive <群别名> on|off")
            return
        enabled = args[1].casefold() in {"on", "开启"}
        yield event.plain_result(await self._set_archive(event, args[0], enabled))

    @nexus.command("archive-status", priority=10)
    async def cmd_archive_status(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        args = self._command_args(event)
        yield event.plain_result(
            message if not allowed else (
                await self._archive_status(event, args[0])
                if args else "用法：/nexus archive-status <群别名>"
            ),
        )

    @nexus.command("relay-confirm", priority=10)
    async def cmd_relay_confirm(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._confirm_relay(event, args[0])
            if args else "用法：/nexus relay-confirm <X-任务ID>",
        )

    @nexus.command("members", priority=10)
    async def cmd_members(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._search_group_members(event, args[0], " ".join(args[1:]))
            if args else "用法：/nexus members <群别名> [关键词]",
        )

    @nexus.command("member-sets", priority=10)
    async def cmd_member_sets(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(await self._list_member_sets(event, args[0] if args else ""))

    @nexus.command("member-set", priority=10)
    async def cmd_member_set(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._get_member_set(event, args[0], args[1])
            if len(args) >= 2 else "用法：/nexus member-set <群别名> <集合名>",
        )

    @nexus.command("moderation-confirm", priority=10)
    async def cmd_moderation_confirm(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._confirm_moderation(event, args[0])
            if args else "用法：/nexus moderation-confirm <M-任务ID>",
        )

    @nexus.command("tasks", priority=10)
    async def cmd_tasks(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(await self._tasks(event, args[0] if args else None))

    @nexus.command("task", priority=10)
    async def cmd_task(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            (
                await self._task_details(event, args[0])
                if args else "用法：/nexus task <任务ID>"
            ),
        )

    @nexus.command("cancel", priority=10)
    async def cmd_cancel(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        if not args:
            yield event.plain_result("用法：/nexus cancel <任务ID>")
        else:
            yield event.plain_result(await self._cancel_task(event, args[0]))

    @nexus.command("collect-start", priority=10)
    async def cmd_collect_start(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        parts = [part.strip() for part in " ".join(args).split("|")]
        if len(parts) < 3:
            yield event.plain_result("用法：/nexus collect-start 群别名|标题|字段1,字段2[,公告][,all]")
            return
        fields = [field.strip() for field in parts[2].replace("，", ",").split(",") if field.strip()]
        announcement = parts[3] if len(parts) > 3 else ""
        mention_all = len(parts) > 4 and parts[4].casefold() in {"all", "@all", "全体"}
        yield event.plain_result(
            await self._start_collection(event, parts[0], parts[1], fields, announcement, mention_all),
        )

    @nexus.command("collect-status", priority=10)
    async def cmd_collect_status(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._collection_status(event, args[0], "", True)
            if args else "用法：/nexus collect-status <任务ID>",
        )

    @nexus.command("collect-stop", priority=10)
    async def cmd_collect_stop(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        args = self._command_args(event)
        yield event.plain_result(
            await self._stop_collection(event, args[0])
            if args
            else "用法：/nexus collect-stop <任务ID>",
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        sender_id = str(event.get_sender_id())
        if sender_id and sender_id == str(event.get_self_id()):
            return
        group_id = str(event.get_group_id())
        message_text = str(event.get_message_str() or "")
        try:
            await self.manager.archive_group_message(
                self._platform_id(event),
                group_id,
                sender_id,
                event.get_sender_name(),
                message_text,
                source_message_id=self._event_source_message_id(event),
            )
        except Exception:
            logger.exception("群枢归档群消息失败")
        try:
            result = await self.manager.capture_active_collection_message(
                self._platform_id(event),
                group_id,
                sender_id,
                event.get_sender_name(),
                message_text,
                source_message_id=self._event_source_message_id(event),
            )
        except Exception:
            logger.exception("群枢捕获 Collection 群消息失败")
            return
        if result is None or not result.get("deterministic_handled"):
            return
        if self.collection_ack:
            missing = result["missing"]
            text = "已记录。" if not missing else f"已记录，目前还缺少：{'、'.join(missing)}"
            yield event.plain_result(text)

    @filter.llm_tool(name="nexus_bind_group")
    async def nexus_bind_group(self, event: AstrMessageEvent, alias: str, group_id: str) -> str:
        """绑定一个可供群枢任务使用的 QQ 群。必须由 operator 在私聊中调用，插件会先验证 Bot 可访问该群。

        Args:
            alias(string): 群别名，例如“班群”或“班委群”。
            group_id(string): QQ 群号。
        """
        return await self._bind_group(event, alias, group_id)

    @filter.llm_tool(name="nexus_list_groups")
    async def nexus_list_groups(self, event: AstrMessageEvent) -> str:
        """列出当前 operator 在本 QQ 平台已绑定的群。只能在私聊中调用。"""
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._groups(event)

    @filter.llm_tool(name="nexus_create_reminder")
    async def nexus_create_reminder(
        self,
        event: AstrMessageEvent,
        group: str,
        run_at: str,
        message: str,
        mention_all: bool = False,
    ) -> str:
        """创建一个持久化的一次性 QQ 群提醒。先把自然语言时间转换为 YYYY-MM-DD HH:MM，再调用本工具；例如“明天下午三点”应转换为明确时间。只能由私聊 operator 或当前群 QQ 群主/管理员调用；群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名或群号。
            run_at(string): 按插件时区解释的 YYYY-MM-DD HH:MM 或 ISO-8601 时间。
            message(string): 到期时发送到群里的提醒内容。
            mention_all(boolean): 是否在提醒前 @全体成员。
        """
        allowed, denied, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_reminder(
                effective_group or group,
                run_at,
                message,
                mention_all,
                self._platform_id(event),
                event.get_sender_id(),
                event.unified_msg_origin,
            )
            local_run_at = format_local_time(
                task["run_at"], self.manager.timezone_name, "minutes",
            )
            return (
                f"已创建提醒任务 {task['id']}，计划时间：{local_run_at} "
                f"（{self.manager.timezone_name}）。"
            )
        except (KeyError, ValueError) as exc:
            return f"创建提醒失败：{exc}"

    @filter.llm_tool(name="nexus_list_tasks")
    async def nexus_list_tasks(self, event: AstrMessageEvent, group: str = "") -> str:
        """列出群枢任务，可按已绑定群别名筛选。私聊 operator 可跨群查看；群内只允许当前群 QQ 群主/管理员查看当前群任务。

        Args:
            group(string): 可选的群别名或群号；留空表示全部任务。
        """
        return await self._tasks(event, group or None)

    @filter.llm_tool(name="nexus_get_task")
    async def nexus_get_task(self, event: AstrMessageEvent, task_id: str) -> str:
        """查看一个群枢任务的详细信息。普通任务可由私聊 operator 或当前群 QQ 群主/管理员查看当前群任务；MODERATION 只能由创建者 moderator 或 AstrBot Admin 查看，不返回原始 JSON 或服务器文件路径。

        Args:
            task_id(string): 任务 ID，例如 D-20260909-001、S-20260909-001 或 K-20260909-001。
        """
        return await self._task_details(event, task_id)

    @filter.llm_tool(name="nexus_create_ddl")
    async def nexus_create_ddl(
        self,
        event: AstrMessageEvent,
        group: str,
        title: str,
        deadline: str,
        remind_before_minutes: list[int],
        message: str = "",
        mention_all: bool = False,
    ) -> str:
        """创建带多个提前提醒的持久化 DDL。只能由私聊 operator 或当前群 QQ 群主/管理员调用；群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名。
            title(string): DDL 标题。
            deadline(string): 按插件时区解释的明确 YYYY-MM-DD HH:MM 时间。
            remind_before_minutes(list[number]): 提前分钟数，例如 [4320, 1440, 180]；必须为整数，0 表示截止时刻提醒，只有明确传入才会创建。
            message(string): 可选的群提醒内容；留空由插件生成。
            mention_all(boolean): 是否 @全体成员。
        """
        allowed, denied, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_ddl(
                effective_group or group, title, deadline, remind_before_minutes, message, mention_all,
                self._platform_id(event), event.get_sender_id(), event.unified_msg_origin,
            )
            skipped = self._payload(task).get("skipped_offsets", [])
            suffix = f"已跳过已过去的提前时间：{', '.join(str(item) for item in skipped)} 分钟。" if skipped else ""
            return f"已创建 DDL 任务 {task['id']}。{suffix}"
        except (KeyError, ValueError) as exc:
            return f"创建 DDL 失败：{exc}"

    @filter.llm_tool(name="nexus_create_recurring_reminder")
    async def nexus_create_recurring_reminder(
        self,
        event: AstrMessageEvent,
        group: str,
        weekdays: list[int],
        time_of_day: str,
        message: str,
        mention_all: bool = False,
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """创建每周周期提醒。仅支持明确的 weekly weekday/time 规则，1=Monday、2=Tuesday、3=Wednesday、4=Thursday、5=Friday、6=Saturday、7=Sunday；只能由私聊 operator 或当前群 QQ 群主/管理员调用；群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名。
            weekdays(list[number]): 星期列表，1=Monday 到 7=Sunday，必须为整数。
            time_of_day(string): 按插件时区解释的 HH:MM。
            message(string): 每次提醒内容。
            mention_all(boolean): 是否 @全体成员。
            start_date(string): 可选的 YYYY-MM-DD 起始日期。
            end_date(string): 可选的 YYYY-MM-DD 结束日期。
        """
        allowed, denied, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_recurring_reminder(
                effective_group or group, weekdays, time_of_day, message, mention_all, start_date, end_date,
                self._platform_id(event), event.get_sender_id(), event.unified_msg_origin,
            )
            return (
                f"已创建周期提醒 {task['id']}，下次执行："
                f"{format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')} "
                f"（{self.manager.timezone_name}）。"
            )
        except (KeyError, ValueError) as exc:
            return f"创建周期提醒失败：{exc}"

    @filter.llm_tool(name="nexus_create_course")
    async def nexus_create_course(
        self,
        event: AstrMessageEvent,
        group: str,
        course_name: str,
        weekdays: list[int],
        start_time: str,
        location: str = "",
        remind_before_minutes: int = 20,
        start_date: str = "",
        end_date: str = "",
        mention_all: bool = False,
    ) -> str:
        """创建每周课程提醒。weekdays 使用 1=Monday 到 7=Sunday；所有时间按插件时区解释，只能由私聊 operator 或当前群 QQ 群主/管理员调用；群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名。
            course_name(string): 课程名称。
            weekdays(list[number]): 上课星期，1=Monday 到 7=Sunday，必须为整数。
            start_time(string): 上课时间 HH:MM。
            location(string): 可选地点。
            remind_before_minutes(number): 提前提醒分钟数，必须为非负整数。
            start_date(string): 可选课程起始日期 YYYY-MM-DD。
            end_date(string): 可选课程结束日期 YYYY-MM-DD。
            mention_all(boolean): 是否 @全体成员。
        """
        allowed, denied, effective_group = await self._authorized_collection_control(event, group)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_course(
                effective_group or group, course_name, weekdays, start_time, location, remind_before_minutes,
                start_date, end_date, mention_all, self._platform_id(event),
                event.get_sender_id(), event.unified_msg_origin,
            )
            return (
                f"已创建课程提醒 {task['id']}，下次提醒："
                f"{format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')} "
                f"（{self.manager.timezone_name}）。"
            )
        except (KeyError, ValueError) as exc:
            return f"创建课程提醒失败：{exc}"

    @filter.llm_tool(name="nexus_schedule_collection_chase")
    async def nexus_schedule_collection_chase(
        self,
        event: AstrMessageEvent,
        task_id: str,
        run_at: str,
        message: str = "",
        repeat_interval_minutes: int = 0,
    ) -> str:
        """为 ACTIVE 信息收集安排未提交成员催办。到时只 @当前未提交成员，不 @全体；repeat_interval_minutes 为 0 表示单次，否则必须至少 60 分钟且不超过 7 天。只能由私聊 operator 或当前群 QQ 群主/管理员调用；群内只能作用于当前群。

        Args:
            task_id(string): ACTIVE COLLECTION 任务 ID。
            run_at(string): 按插件时区解释的明确 YYYY-MM-DD HH:MM 时间。
            message(string): 可选催办内容。
            repeat_interval_minutes(number): 重复间隔分钟数，必须为整数，0 或至少 60。
        """
        allowed, denied, _effective_group = await self._authorized_collection_control(event)
        if not allowed:
            return denied
        try:
            current_task = await self.manager.get_task(
                task_id, self._platform_id(event),
            )
            if not event.is_private_chat() and current_task["group_id"] != str(event.get_group_id()):
                return "群内控制只能作用于当前群，不能跨群操作。"
            task = await self.manager.schedule_collection_chase(
                task_id, run_at, message, repeat_interval_minutes,
                self._platform_id(event), event.get_sender_id(), event.unified_msg_origin,
            )
            return (
                f"已安排收集催办 {task['id']}，执行时间："
                f"{format_local_time(task['run_at'], self.manager.timezone_name, 'minutes')}。"
            )
        except (KeyError, ValueError) as exc:
            return f"安排催办失败：{exc}"

    @filter.llm_tool(name="nexus_cancel_task")
    async def nexus_cancel_task(self, event: AstrMessageEvent, task_id: str) -> str:
        """取消一个尚未执行的一次性提醒、PENDING relay 或 PENDING 群管理操作，或取消 ACTIVE 的 DDL、周期、课程、每周总结父任务并级联取消其未执行子提醒。信息收集必须使用 nexus_stop_collection 结束；普通任务可由私聊 operator 或当前群 QQ 群主/管理员操作当前群任务，MODERATION 只能由创建者 moderator 或 AstrBot Admin 取消。

        Args:
            task_id(string): 要取消的任务 ID，例如 R-20260909-001。
        """
        return await self._cancel_task(event, task_id)

    @filter.llm_tool(name="nexus_start_collection")
    async def nexus_start_collection(
        self,
        event: AstrMessageEvent,
        group: str,
        title: str,
        fields: list[str],
        announcement: str = "",
        mention_all: bool = False,
        target_member_set: str = "",
        ai_extraction: bool = False,
        chase_at: str = "",
        deadline: str = "",
        missing_default_field: str = "",
        missing_default_value: str = "",
        auto_export: bool = False,
    ) -> str:
        """在已绑定 QQ 群启动一次信息收集。群消息会先持久化到 workflow history，标准字段立即写入；开启 ai_extraction 后由 chase/deadline 或状态 refresh 批量增量分析，不会逐消息调用 LLM。只能由私聊 operator 或当前群 QQ 群主/管理员调用；群内 group 参数必须是当前群。

        Args:
            group(string): 已绑定群别名或群号，例如“班群”。
            title(string): 收集任务标题。
            fields(list[string]): 要收集的字段列表，例如 ["姓名", "离校时间", "返校时间"]。
            announcement(string): 可选的群公告补充说明。
            mention_all(boolean): 是否在启动公告中 @全体成员。
            target_member_set(string): 可选的群内成员集合名称；创建时会 snapshot 成员，后续名单变化不影响本次收集。
            ai_extraction(boolean): 是否开启 checkpoint semantic analysis；默认 false。开启后创建时绑定当前 AstrBot LLM Provider ID，只保存 ID，不保存凭据。
            chase_at(string): 可选的 checkpoint 时间，按插件时区解释的 YYYY-MM-DD HH:MM，必须早于 deadline。
            deadline(string): 可选的截止时间，按插件时区解释的 YYYY-MM-DD HH:MM。
            missing_default_field(string): 截止时对完全没有 entry 的成员使用的字段，必须属于 fields。
            missing_default_value(string): 与 missing_default_field 同时提供的缺省值。
            auto_export(boolean): 是否在 deadline 自动生成 XLSX 并尝试私聊回传；没有 deadline 时不能为 true。
        """
        return await self._start_collection(
            event,
            group,
            title,
            fields,
            announcement,
            mention_all,
            target_member_set,
            ai_extraction,
            chase_at,
            deadline,
            missing_default_field,
            missing_default_value,
            auto_export,
        )

    @filter.llm_tool(name="nexus_collection_status")
    async def nexus_collection_status(
        self,
        event: AstrMessageEvent,
        task_id: str = "",
        group: str = "",
        refresh: bool = True,
    ) -> str:
        """查看信息收集任务的提交人数、群成员数、未提交人数和状态；默认会先对 cursor 后的新 workflow messages 做一次增量 refresh。可用 task_id、group 或当前会话中的 Collection 线索定位；只能由私聊 operator 或当前群 QQ 群主/管理员调用。

        Args:
            task_id(string): 可选的收集任务 ID，例如 C-20260909-001。
            group(string): 可选的已绑定群别名；群内调用时只能是当前群。
            refresh(boolean): 是否先执行一次增量 checkpoint，默认 true；无新增消息时不会调用 LLM。
        """
        return await self._collection_status(event, task_id, group, refresh)

    @filter.llm_tool(name="nexus_stop_collection")
    async def nexus_stop_collection(self, event: AstrMessageEvent, task_id: str) -> str:
        """结束信息收集，生成 XLSX，并尝试通过 QQ 私聊回传给任务创建者。文件回传失败不会回滚导出结果。只能由私聊 operator 或当前群 QQ 群主/管理员调用，群内只能结束当前群任务。

        Args:
            task_id(string): 要结束的收集任务 ID。
        """
        return await self._stop_collection(event, task_id)

    @filter.llm_tool(name="nexus_set_archive")
    async def nexus_set_archive(self, event: AstrMessageEvent, group: str, enabled: bool) -> str:
        """开启或关闭已绑定 QQ 群的文本消息归档。默认关闭，开启不会回溯历史；只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号。
            enabled(boolean): true 开启从现在开始归档，false 停止新增归档但保留已有数据。
        """
        return await self._set_archive(event, group, enabled)

    @filter.llm_tool(name="nexus_archive_status")
    async def nexus_archive_status(self, event: AstrMessageEvent, group: str) -> str:
        """查看已绑定群的文本归档开关和消息数量，即使当前关闭也会显示已有数据。只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号。
        """
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._archive_status(event, group)

    @filter.llm_tool(name="nexus_search_messages")
    async def nexus_search_messages(
        self,
        event: AstrMessageEvent,
        group: str,
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
        limit: int = 50,
    ) -> str:
        """查询已绑定群的文本归档，时间按插件时区解释；只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号，不能查询未绑定群。
            keyword(string): 可选关键词，使用简单文本匹配；留空表示最近消息。
            start_time(string): 可选起始时间，YYYY-MM-DD HH:MM 或 ISO-8601。
            end_time(string): 可选结束时间，YYYY-MM-DD HH:MM 或 ISO-8601。
            limit(number): 返回 1 到 100 条消息，默认 50。
        """
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._search_messages(
            event, group, keyword, start_time, end_time, limit,
        )

    @filter.llm_tool(name="nexus_clear_archive")
    async def nexus_clear_archive(
        self, event: AstrMessageEvent, group: str, confirm: bool = False,
    ) -> str:
        """清空已绑定群的全部文本归档。永久删除；只有用户明确确认“删除/清空该群归档”后才能传 confirm=true。只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号。
            confirm(boolean): 只有用户明确确认永久删除时才传 true，否则必须保持 false。
        """
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        if confirm is not True:
            return "这是永久删除操作，请明确确认后再执行。"
        try:
            count = await self.manager.clear_archive(group, self._platform_id(event))
            return f"已清空「{group}」的 {count} 条消息归档。"
        except (KeyError, ValueError) as exc:
            return f"清空归档失败：{exc}"

    @filter.llm_tool(name="nexus_summarize_group")
    async def nexus_summarize_group(
        self,
        event: AstrMessageEvent,
        group: str,
        start_time: str = "",
        end_time: str = "",
        focus: str = "",
    ) -> str:
        """根据已绑定群的文本归档生成总结。默认总结最近 7 天；只依据记录，不会自动创建任务；只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号。
            start_time(string): 可选起始时间，按插件时区解释。
            end_time(string): 可选结束时间，按插件时区解释。
            focus(string): 可选关注重点，例如“DDL 和待办候选”。
        """
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._summarize_group(
            event, group, start_time, end_time, focus,
        )

    @filter.llm_tool(name="nexus_create_weekly_summary")
    async def nexus_create_weekly_summary(
        self,
        event: AstrMessageEvent,
        group: str,
        weekday: int,
        time_of_day: str,
        lookback_days: int = 7,
        focus: str = "",
    ) -> str:
        """创建自动每周群聊总结，按计划时间回看固定窗口并私聊发送给创建者。星期 1=Monday 到 7=Sunday；只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号。
            weekday(number): 每周星期几，1=Monday 到 7=Sunday。
            time_of_day(string): 按插件时区解释的 HH:MM。
            lookback_days(number): 回看天数，1 到 30，默认 7。
            focus(string): 可选关注重点。
        """
        return await self._create_weekly_summary(
            event, group, weekday, time_of_day, lookback_days, focus,
        )

    @filter.llm_tool(name="nexus_prepare_relay")
    async def nexus_prepare_relay(
        self,
        event: AstrMessageEvent,
        target_group: str,
        content: str,
        mention_all: bool = False,
        source_note: str = "",
        member_set: str = "",
    ) -> str:
        """准备向另一个已绑定群转述内容。即使用户说“发到某群”，第一步也只能调用本工具展示 preview；绝不能在同一轮自动确认或发送。用户明确确认后，再调用 nexus_confirm_relay。只能在私聊 operator 中调用。

        Args:
            target_group(string): 已绑定的目标群别名或群号。
            content(string): 待转述内容，最多 6000 个字符。
            mention_all(boolean): 是否在确认发送时 @全体。
            source_note(string): 可选来源备注，仅用于 preview 和记录。
            member_set(string): 可选群内成员集合名称；会在 preview 时 snapshot 当前仍在群成员。与 mention_all 互斥。
        """
        return await self._prepare_relay(
            event, target_group, content, mention_all, source_note,
            member_set,
        )

    @filter.llm_tool(name="nexus_confirm_relay")
    async def nexus_confirm_relay(self, event: AstrMessageEvent, task_id: str) -> str:
        """发送此前已 prepare 且仍为 PENDING 的 relay。只有用户在看到 preview 后明确说“确认发送”才能调用；不能用于首次请求的自动发送。只能在私聊 operator 中调用。

        Args:
            task_id(string): nexus_prepare_relay 返回的 Relay ID，例如 X-20260909-001。
        """
        return await self._confirm_relay(event, task_id)

    @filter.llm_tool(name="nexus_search_group_members")
    async def nexus_search_group_members(
        self, event: AstrMessageEvent, group: str, query: str = "", limit: int = 20,
    ) -> str:
        """实时查询已绑定 QQ 群成员。查询只使用当前群成员列表，不查询归档；只能由私聊 operator 或当前群 QQ 群主/管理员调用，群内只能查询当前群。

        Args:
            group(string): 已绑定群别名或群号。
            query(string): 可选关键词，对 QQ 号、群名片、昵称做不区分大小写的包含匹配。
            limit(number): 返回 1 到 50 条，默认 20。
        """
        return await self._search_group_members(event, group, query, limit)

    @filter.llm_tool(name="nexus_set_member_set")
    async def nexus_set_member_set(
        self,
        event: AstrMessageEvent,
        group: str,
        name: str,
        members: list[str],
        mode: str = "replace",
    ) -> str:
        """创建或更新一个 group-scoped 成员集合。成员引用必须是当前群真实成员的 QQ 号、精确群名片或精确昵称；有歧义时必须改用 QQ 号。不能加入 Bot 或机器人；只能由私聊 operator 或当前群 QQ 群主/管理员调用，群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名。
            name(string): 成员集合名称，1 到 40 个字符。
            members(list[string]): 成员 QQ 号、精确群名片或精确昵称列表，重复 ID 会去重。
            mode(string): replace、add 或 remove；replace/add/remove 后集合都不能为空。
        """
        return await self._set_member_set(event, group, name, members, mode)

    @filter.llm_tool(name="nexus_list_member_sets")
    async def nexus_list_member_sets(
        self, event: AstrMessageEvent, group: str = "",
    ) -> str:
        """列出已绑定群的成员集合及人数。留空 group 表示私聊 operator 列出当前平台全部集合；群内只能列出当前群，且需要 QQ 群主/管理员权限。

        Args:
            group(string): 可选已绑定群别名。
        """
        return await self._list_member_sets(event, group)

    @filter.llm_tool(name="nexus_get_member_set")
    async def nexus_get_member_set(
        self, event: AstrMessageEvent, group: str, name: str,
    ) -> str:
        """查看一个 group-scoped 成员集合，并尽力标出已退群成员；只能由私聊 operator 或当前群 QQ 群主/管理员调用，群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名。
            name(string): 成员集合名称。
        """
        return await self._get_member_set(event, group, name)

    @filter.llm_tool(name="nexus_delete_member_set")
    async def nexus_delete_member_set(
        self, event: AstrMessageEvent, group: str, name: str, confirm: bool = False,
    ) -> str:
        """删除一个成员集合。只有用户明确确认删除该名单后才能传 confirm=true；这是永久删除集合数据的操作，不影响历史任务；只能由私聊 operator 或当前群 QQ 群主/管理员调用，群内只能作用于当前群。

        Args:
            group(string): 已绑定群别名。
            name(string): 成员集合名称。
            confirm(boolean): 只有用户明确确认删除名单时才传 true，否则必须保持 false。
        """
        return await self._delete_member_set(event, group, name, confirm)

    @filter.llm_tool(name="nexus_set_member_identity")
    async def nexus_set_member_identity(
        self,
        event: AstrMessageEvent,
        group: str,
        member: str,
        name: str = "",
        student_id: str = "",
    ) -> str:
        """保存一个当前群成员的姓名或学号。仅允许已绑定群的私聊 operator 或当前群 QQ 群主/管理员调用；成员必须实时存在于该群，且只能按 QQ 号、精确群名片或精确昵称解析。

        Args:
            group(string): 已绑定群别名或群号；群内调用时只能是当前群。
            member(string): 成员 QQ 号、精确群名片或精确昵称；歧义时请使用 QQ 号。
            name(string): 可选的姓名。
            student_id(string): 可选的学号。
        """
        return await self._set_member_identity(event, group, member, name, student_id)

    @filter.llm_tool(name="nexus_get_member_identity")
    async def nexus_get_member_identity(
        self, event: AstrMessageEvent, group: str, member: str,
    ) -> str:
        """查询一个已绑定群成员的姓名/学号身份信息。仅允许私聊 operator 或当前群 QQ 群主/管理员调用，不查询未绑定群。

        Args:
            group(string): 已绑定群别名或群号；群内调用时只能是当前群。
            member(string): 成员 QQ 号、精确群名片或精确昵称；歧义时请使用 QQ 号。
        """
        return await self._get_member_identity(event, group, member)

    @filter.llm_tool(name="nexus_list_member_identities")
    async def nexus_list_member_identities(
        self, event: AstrMessageEvent, group: str = "",
    ) -> str:
        """列出已绑定群中已保存的成员身份信息。留空 group 时仅私聊 operator 可列出当前平台各绑定群；群内调用只能列出当前群。

        Args:
            group(string): 可选的已绑定群别名或群号。
        """
        return await self._list_member_identities(event, group)

    @filter.llm_tool(name="nexus_prepare_moderation")
    async def nexus_prepare_moderation(
        self,
        event: AstrMessageEvent,
        group: str,
        action: str,
        target: str,
        duration_seconds: int = 0,
        reject_add_request: bool = False,
        reason: str = "",
    ) -> str:
        """准备对一个群内单独成员执行 mute、unmute 或 kick。即使用户直接说“把张三禁言”，第一步也只能调用本工具展示 preview，绝不能同一轮自动 confirm；用户明确确认后才调用 nexus_confirm_moderation。群管理默认关闭、需要独立 moderator 权限，不支持成员集合或批量操作。

        Args:
            group(string): 已绑定群别名。
            action(string): 只能是 mute、unmute 或 kick。
            target(string): QQ 号、精确群名片或精确昵称；歧义时必须使用 QQ 号。
            duration_seconds(number): mute 为 60 到 2592000 秒；unmute 必须为 0；kick 忽略该值。
            reject_add_request(boolean): kick 后是否拒绝再次加群申请。
            reason(string): 可选操作原因，仅用于预览和审计记录。
        """
        return await self._prepare_moderation(
            event, group, action, target, duration_seconds, reject_add_request, reason,
        )

    @filter.llm_tool(name="nexus_confirm_moderation")
    async def nexus_confirm_moderation(
        self, event: AstrMessageEvent, task_id: str,
    ) -> str:
        """执行此前已 prepare 且仍为 PENDING 的群管理 preview。只有创建该 preview 的 moderator 在看到预览后明确说“确认执行”才能调用；preview 只有 10 分钟有效，操作失败不会自动重试。

        Args:
            task_id(string): nexus_prepare_moderation 返回的 Action ID，例如 M-20260909-001。
        """
        return await self._confirm_moderation(event, task_id)
