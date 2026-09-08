"""AstrBot entry point for 微光·群枢 / Lumielle Nexus."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools

from core import TaskManager
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
        )
        operator_ids = self.config.get("operator_ids", []) or []
        self.operator_ids = {str(value).strip() for value in operator_ids if str(value).strip()}
        interval = int(self.config.get("scheduler_interval_seconds", 15))
        self.scheduler_interval_seconds = max(5, min(interval, 3600))
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
    def _command_args(event: AstrMessageEvent) -> list[str]:
        text = str(event.get_message_str() or "").strip()
        parts = text.split()
        return parts[2:] if len(parts) >= 2 else []

    @staticmethod
    def _task_line(task: dict[str, Any]) -> str:
        payload = LumielleNexus._payload(task)
        label = payload.get("title") or payload.get("message") or task["type"]
        return f"{task['id']} [{task['status']}] {task['group_alias']}：{label}"

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
                return f"收集任务 {task['id']} 已创建，但群公告发送失败：{exc}"
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
            submitted = status["submitted_count"]
            lines = [
                f"{task['id']}：{self._payload(task).get('title', task['group_alias'])}",
                f"状态：{task['status']}",
                f"已提交：{submitted} 人",
            ]
            if members is not None:
                lines.extend(
                    [
                        f"群成员：{len(members)} 人",
                        f"未提交：{max(0, len(members) - submitted)} 人",
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
                "member_count": len(members) if members is not None else None,
                "member_error": member_error or None,
                "upload_error": upload_error or None,
            }
            await self.manager.complete_collection(task["id"], result)
            summary = f"统计已结束：{task['id']}，提交 {len(snapshot['entries'])} 人。Excel 已保存：{output}"
            if upload_error:
                summary += f"\n但 QQ 文件回传失败：{upload_error}"
            return summary
        except (KeyError, ValueError) as exc:
            return f"结束失败：{exc}"

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                for task in await self.manager.due_tasks():
                    try:
                        payload = self._payload(task)
                        adapter = QQAdapter(self.context, task["platform_id"])
                        if payload.get("mention_all"):
                            await adapter.send_group_at_all(task["group_id"], payload["message"])
                        else:
                            await adapter.send_group_text(task["group_id"], payload["message"])
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
            "/nexus cancel <任务ID>\n"
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

    @nexus.command("tasks", priority=10)
    async def cmd_tasks(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        allowed, message = self._authorized_for_control(event)
        args = self._command_args(event)
        yield event.plain_result(message if not allowed else await self._tasks(event, args[0] if args else None))

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
        result = await self.manager.process_collection_message(
            self._platform_id(event),
            str(event.get_group_id()),
            sender_id,
            event.get_sender_name(),
            event.get_message_str(),
        )
        if result is None:
            return
        event.stop_event()
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
            return f"已创建提醒任务 {task['id']}，计划时间：{task['run_at']}。"
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

    @filter.llm_tool(name="nexus_cancel_task")
    async def nexus_cancel_task(self, event: AstrMessageEvent, task_id: str) -> str:
        """取消一个尚未执行的一次性提醒或仍在进行的收集任务。只能在私聊 operator 中调用。

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
