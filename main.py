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
    from .core import TaskManager, collection_member_stats, format_local_time
    from .exporter import export_collection
    from .qq_adapter import QQAdapter, QQAdapterError
    from .storage import Storage
else:
    from core import TaskManager, collection_member_stats, format_local_time
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
                await self.manager.materialize_due_schedules()
                for task in await self.manager.due_tasks():
                    try:
                        await self._execute_reminder(task)
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

    async def _execute_reminder(self, task: dict[str, Any]) -> None:
        payload = self._payload(task)
        if payload.get("kind") == "collection_chase":
            await self._execute_collection_chase(task, payload)
            return
        adapter = QQAdapter(self.context, task["platform_id"])
        if payload.get("mention_all"):
            await adapter.send_group_at_all(task["group_id"], payload["message"])
        else:
            await adapter.send_group_text(task["group_id"], payload["message"])

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
                await self.manager.schedule_collection_chase(
                    collection["id"],
                    datetime.now(timezone.utc) + timedelta(minutes=interval),
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
        result = await self.manager.process_collection_message(
            self._platform_id(event),
            str(event.get_group_id()),
            sender_id,
            event.get_sender_name(),
            event.get_message_str(),
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
        """取消一个尚未执行的一次性提醒，或取消 ACTIVE 的 DDL、周期提醒、课程父任务并级联取消其未执行子提醒。信息收集必须使用 nexus_stop_collection 结束；只能在私聊 operator 中调用。

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
