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
        SUMMARY_PRIVATE_CHUNK_CHARS,
        TaskManager,
        clamp_archive_max_message_chars,
        clamp_archive_retention_days,
        clamp_scheduler_interval,
        collection_member_stats,
        format_local_time,
        generate_group_summary,
        next_interval_occurrence,
    )
    from .exporter import export_collection
    from .qq_adapter import QQAdapter, QQAdapterError
    from .storage import Storage
else:
    from core import (
        SUMMARY_PRIVATE_CHUNK_CHARS,
        TaskManager,
        clamp_archive_max_message_chars,
        clamp_archive_retention_days,
        clamp_scheduler_interval,
        collection_member_stats,
        format_local_time,
        generate_group_summary,
        next_interval_occurrence,
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
        else:
            label = f"{payload.get('message', task_type)}，时间 {format_local_time(task.get('run_at'), self.manager.timezone_name, 'minutes')}"
        return f"{task['id']} [{task['status']}] {task['group_alias']}：{label}"

    @staticmethod
    def _weekdays_text(weekdays: list[int]) -> str:
        names = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
        return "、".join(names.get(int(day), str(day)) for day in weekdays)

    async def _task_details(self, event: AstrMessageEvent, task_id: str) -> str:
        try:
            task = await self.manager.get_task(task_id, self._platform_id(event))
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
                    f"@全体：{'是' if payload.get('mention_all') else '否'}",
                    f"内容：{payload.get('content', '')}",
                    f"来源备注：{payload.get('source_note') or '无'}",
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
            metadata = (
                "【群聊总结】\n"
                f"时间范围：{format_local_time(snapshot['window_start'], self.manager.timezone_name, 'minutes')}"
                f" 至 {format_local_time(snapshot['window_end'], self.manager.timezone_name, 'minutes')}\n"
                f"消息数：{snapshot['message_count']}\n"
                f"活跃成员数：{snapshot['unique_sender_count']}"
            )
            if snapshot["truncated"]:
                metadata += "\n消息量较大，本次总结使用最近 2000 条文本消息。"
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
    ) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            task = await self.manager.prepare_relay(
                target_group, content, mention_all, source_note,
                self._platform_id(event), event.get_sender_id(), event.unified_msg_origin,
            )
            payload = self._payload(task)
            return (
                f"待发送到：{task['group_alias']}\n"
                f"@全体：{'是' if payload.get('mention_all') else '否'}\n\n"
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
            task = await self.manager.confirm_relay(task_id, self._platform_id(event))
            payload = self._payload(task)
            try:
                adapter = self._adapter(event)
                if payload.get("mention_all"):
                    await adapter.send_group_at_all(task["group_id"], payload["content"])
                else:
                    await adapter.send_group_text(task["group_id"], payload["content"])
            except QQAdapterError as exc:
                await self.manager.finish_relay(task["id"], False, str(exc))
                return f"转述发送失败：{exc}。该 Relay 已标记失败，请重新准备。"
            await self.manager.finish_relay(task["id"], True)
            return f"已将 Relay {task['id']} 发送到「{task['group_alias']}」。"
        except (KeyError, ValueError) as exc:
            return f"确认转述失败：{exc}"

    async def _tasks(self, event: AstrMessageEvent, group: str | None = None) -> str:
        tasks = await self.manager.list_tasks(self._platform_id(event), group or None)
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
    ) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            task = await self.manager.start_collection(
                group,
                title,
                fields,
                announcement,
                mention_all,
                self._platform_id(event),
                event.get_sender_id(),
                event.unified_msg_origin,
            )
            payload = self._payload(task)
            notice = payload.get("announcement") or (
                f"【{payload['title']}】\n\n请提交以下信息：\n\n"
                + "\n".join(f"{field}：" for field in payload["fields"])
                + "\n\n直接在群内按以上格式发送即可。"
            )
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

    async def _collection_status(self, event: AstrMessageEvent, task_id: str) -> str:
        try:
            status = await self.manager.collection_status(task_id, self._platform_id(event))
            task = status["task"]
            members: list[dict[str, Any]] | None = None
            member_error = ""
            try:
                members = await self._adapter(event).get_group_member_list(task["group_id"])
            except QQAdapterError as exc:
                member_error = str(exc)
            if members is not None:
                member_stats = collection_member_stats(
                    members,
                    status["entries"],
                    self_id=str(event.get_self_id()),
                )
                submitted = len(member_stats["submitted_ids"])
            else:
                member_stats = None
                submitted = status["submitted_count"]
            lines = [
                f"{task['id']}：{self._payload(task).get('title', task['group_alias'])}",
                f"状态：{task['status']}",
                f"已提交：{submitted} 人",
            ]
            if members is not None:
                lines.extend(
                    [
                        f"群成员：{len(member_stats['eligible_ids'])} 人",
                        f"未提交：{len(member_stats['missing_ids'])} 人",
                    ],
                )
            else:
                lines.append(f"群成员数：暂时无法获取（{member_error}）")
            return "\n".join(lines)
        except (KeyError, ValueError) as exc:
            return f"查询失败：{exc}"

    async def _stop_collection(self, event: AstrMessageEvent, task_id: str) -> str:
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            snapshot = await self.manager.stop_collection(task_id, self._platform_id(event))
            task = snapshot["task"]
            members: list[dict[str, Any]] | None = None
            member_error = ""
            try:
                members = await self._adapter(event).get_group_member_list(task["group_id"])
            except QQAdapterError as exc:
                member_error = str(exc)
            export_task = dict(task)
            export_task["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                output = export_collection(
                    self.data_dir / "exports",
                    export_task,
                    snapshot["entries"],
                    members,
                    self_id=str(event.get_self_id()),
                    timezone_name=self.manager.timezone_name,
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
                + ("\n消息量较大，本次总结使用最近 2000 条文本消息。" if snapshot["truncated"] else "")
                + f"\n\n{summary_body}"
            )
            cached = {
                "summary_text": summary_text,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "window_start": snapshot["window_start"],
                "window_end": snapshot["window_end"],
            }
            await self.manager.update_reminder_result(task["id"], cached)
        await QQAdapter(self.context, task["platform_id"]).send_private_text_chunks(
            task["creator_id"], summary_text, SUMMARY_PRIVATE_CHUNK_CHARS,
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
        adapter = QQAdapter(self.context, task["platform_id"])
        members = await adapter.get_group_member_list(task["group_id"])
        try:
            self_id = str((await adapter.get_login_info()).get("user_id") or "")
        except QQAdapterError:
            self_id = None
        stats = collection_member_stats(members, status["entries"], self_id=self_id)
        missing_ids = sorted(stats["missing_ids"])
        if not missing_ids:
            return
        await adapter.send_group_at_members(task["group_id"], missing_ids, payload["message"])
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

    @nexus.command("tasks", priority=10)
    async def cmd_tasks(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        args = self._command_args(event)
        yield event.plain_result(message if not allowed else await self._tasks(event, args[0] if args else None))

    @nexus.command("task", priority=10)
    async def cmd_task(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        args = self._command_args(event)
        yield event.plain_result(
            message if not allowed else (
                await self._task_details(event, args[0])
                if args else "用法：/nexus task <任务ID>"
            ),
        )

    @nexus.command("cancel", priority=10)
    async def cmd_cancel(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        args = self._command_args(event)
        if not allowed:
            yield event.plain_result(message)
        elif not args:
            yield event.plain_result("用法：/nexus cancel <任务ID>")
        else:
            try:
                task = await self.manager.cancel_task(args[0], self._platform_id(event))
                yield event.plain_result(f"已取消任务：{task['id']}")
            except (KeyError, ValueError) as exc:
                yield event.plain_result(f"取消失败：{exc}")

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
        allowed, message = self._authorized_for_control(event)
        args = self._command_args(event)
        yield event.plain_result(
            message if not allowed else (
                await self._collection_status(event, args[0])
                if args else "用法：/nexus collect-status <任务ID>"
            ),
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
        result = await self.manager.process_collection_message(
            self._platform_id(event),
            group_id,
            sender_id,
            event.get_sender_name(),
            message_text,
        )
        if result is None:
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
        """创建一个持久化的一次性 QQ 群提醒。先把自然语言时间转换为 YYYY-MM-DD HH:MM，再调用本工具；例如“明天下午三点”应转换为明确时间。只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号。
            run_at(string): 按插件时区解释的 YYYY-MM-DD HH:MM 或 ISO-8601 时间。
            message(string): 到期时发送到群里的提醒内容。
            mention_all(boolean): 是否在提醒前 @全体成员。
        """
        allowed, denied = self._authorized_for_control(event)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_reminder(
                group,
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
        """列出当前 operator 的群枢任务，可按已绑定群别名筛选。只能在私聊中调用。

        Args:
            group(string): 可选的群别名或群号；留空表示全部任务。
        """
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._tasks(event, group or None)

    @filter.llm_tool(name="nexus_get_task")
    async def nexus_get_task(self, event: AstrMessageEvent, task_id: str) -> str:
        """查看一个群枢任务的详细信息。只能在私聊 operator 中调用，不返回原始 JSON 或服务器文件路径。

        Args:
            task_id(string): 任务 ID，例如 D-20260909-001、S-20260909-001 或 K-20260909-001。
        """
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._task_details(event, task_id)

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
        """创建带多个提前提醒的持久化 DDL。只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名。
            title(string): DDL 标题。
            deadline(string): 按插件时区解释的明确 YYYY-MM-DD HH:MM 时间。
            remind_before_minutes(list[number]): 提前分钟数，例如 [4320, 1440, 180]；必须为整数，0 表示截止时刻提醒，只有明确传入才会创建。
            message(string): 可选的群提醒内容；留空由插件生成。
            mention_all(boolean): 是否 @全体成员。
        """
        allowed, denied = self._authorized_for_control(event)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_ddl(
                group, title, deadline, remind_before_minutes, message, mention_all,
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
        """创建每周周期提醒。仅支持明确的 weekly weekday/time 规则，1=Monday、2=Tuesday、3=Wednesday、4=Thursday、5=Friday、6=Saturday、7=Sunday；只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名。
            weekdays(list[number]): 星期列表，1=Monday 到 7=Sunday，必须为整数。
            time_of_day(string): 按插件时区解释的 HH:MM。
            message(string): 每次提醒内容。
            mention_all(boolean): 是否 @全体成员。
            start_date(string): 可选的 YYYY-MM-DD 起始日期。
            end_date(string): 可选的 YYYY-MM-DD 结束日期。
        """
        allowed, denied = self._authorized_for_control(event)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_recurring_reminder(
                group, weekdays, time_of_day, message, mention_all, start_date, end_date,
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
        """创建每周课程提醒。weekdays 使用 1=Monday 到 7=Sunday；所有时间按插件时区解释，只能在私聊 operator 中调用。

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
        allowed, denied = self._authorized_for_control(event)
        if not allowed:
            return denied
        try:
            task = await self.manager.create_course(
                group, course_name, weekdays, start_time, location, remind_before_minutes,
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
        """为 ACTIVE 信息收集安排未提交成员催办。到时只 @当前未提交成员，不 @全体；repeat_interval_minutes 为 0 表示单次，否则必须至少 60 分钟且不超过 7 天。只能在私聊 operator 中调用。

        Args:
            task_id(string): ACTIVE COLLECTION 任务 ID。
            run_at(string): 按插件时区解释的明确 YYYY-MM-DD HH:MM 时间。
            message(string): 可选催办内容。
            repeat_interval_minutes(number): 重复间隔分钟数，必须为整数，0 或至少 60。
        """
        allowed, denied = self._authorized_for_control(event)
        if not allowed:
            return denied
        try:
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
        """取消一个尚未执行的一次性提醒、PENDING relay，或取消 ACTIVE 的 DDL、周期、课程、每周总结父任务并级联取消其未执行子提醒。信息收集必须使用 nexus_stop_collection 结束；只能在私聊 operator 中调用。

        Args:
            task_id(string): 要取消的任务 ID，例如 R-20260909-001。
        """
        allowed, message = self._authorized_for_control(event)
        if not allowed:
            return message
        try:
            task = await self.manager.cancel_task(task_id, self._platform_id(event))
            return f"已取消任务：{task['id']}"
        except (KeyError, ValueError) as exc:
            return f"取消失败：{exc}"

    @filter.llm_tool(name="nexus_start_collection")
    async def nexus_start_collection(
        self,
        event: AstrMessageEvent,
        group: str,
        title: str,
        fields: list[str],
        announcement: str = "",
        mention_all: bool = False,
    ) -> str:
        """在已绑定 QQ 群启动一次信息收集。创建后插件会在群里发送标题、字段格式和说明；同一群同时只能有一个 active collection。只能在私聊 operator 中调用。

        Args:
            group(string): 已绑定群别名或群号，例如“班群”。
            title(string): 收集任务标题。
            fields(list[string]): 要收集的字段列表，例如 ["姓名", "离校时间", "返校时间"]。
            announcement(string): 可选的群公告补充说明。
            mention_all(boolean): 是否在启动公告中 @全体成员。
        """
        return await self._start_collection(
            event,
            group,
            title,
            fields,
            announcement,
            mention_all,
        )

    @filter.llm_tool(name="nexus_collection_status")
    async def nexus_collection_status(self, event: AstrMessageEvent, task_id: str) -> str:
        """查看信息收集任务的提交人数、尽力获取的群成员数、未提交人数和状态。只能在私聊 operator 中调用。

        Args:
            task_id(string): 收集任务 ID，例如 C-20260909-001。
        """
        allowed, message = self._authorized_for_control(event)
        return message if not allowed else await self._collection_status(event, task_id)

    @filter.llm_tool(name="nexus_stop_collection")
    async def nexus_stop_collection(self, event: AstrMessageEvent, task_id: str) -> str:
        """结束信息收集，生成 XLSX，并尝试通过 QQ 私聊回传给任务创建者。文件回传失败不会回滚导出结果。只能在私聊 operator 中调用。

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
    ) -> str:
        """准备向另一个已绑定群转述内容。即使用户说“发到某群”，第一步也只能调用本工具展示 preview；绝不能在同一轮自动确认或发送。用户明确确认后，再调用 nexus_confirm_relay。只能在私聊 operator 中调用。

        Args:
            target_group(string): 已绑定的目标群别名或群号。
            content(string): 待转述内容，最多 6000 个字符。
            mention_all(boolean): 是否在确认发送时 @全体。
            source_note(string): 可选来源备注，仅用于 preview 和记录。
        """
        return await self._prepare_relay(
            event, target_group, content, mention_all, source_note,
        )

    @filter.llm_tool(name="nexus_confirm_relay")
    async def nexus_confirm_relay(self, event: AstrMessageEvent, task_id: str) -> str:
        """发送此前已 prepare 且仍为 PENDING 的 relay。只有用户在看到 preview 后明确说“确认发送”才能调用；不能用于首次请求的自动发送。只能在私聊 operator 中调用。

        Args:
            task_id(string): nexus_prepare_relay 返回的 Relay ID，例如 X-20260909-001。
        """
        return await self._confirm_relay(event, task_id)
