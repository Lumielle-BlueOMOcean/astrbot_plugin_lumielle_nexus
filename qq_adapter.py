"""aiocqhttp and OneBot v11 operations for Lumielle Nexus."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

INLINE_FILE_MAX_BYTES = 4 * 1024 * 1024


class QQAdapterError(RuntimeError):
    """Readable error raised when an aiocqhttp action cannot be completed."""


class QQAdapter:
    def __init__(self, context: Any, platform_id: str) -> None:
        self.context = context
        self.platform_id = str(platform_id)

    def _platform(self) -> Any:
        getter = getattr(self.context, "get_platform_inst", None)
        platform = getter(self.platform_id) if callable(getter) else None
        if platform is None:
            manager = getattr(self.context, "platform_manager", None)
            instances = getattr(manager, "get_insts", lambda: [])()
            for candidate in instances:
                meta = candidate.meta() if callable(getattr(candidate, "meta", None)) else None
                if meta and str(getattr(meta, "id", "")) == self.platform_id:
                    platform = candidate
                    break
        if platform is None:
            raise QQAdapterError(f"未找到平台实例：{self.platform_id}")
        meta = platform.meta() if callable(getattr(platform, "meta", None)) else None
        if meta and getattr(meta, "name", "") != "aiocqhttp":
            raise QQAdapterError("当前平台不是受支持的 aiocqhttp")
        return platform

    def _client(self) -> Any:
        platform = self._platform()
        get_client = getattr(platform, "get_client", None)
        client = get_client() if callable(get_client) else getattr(platform, "bot", None)
        if client is None:
            client = getattr(platform, "client", None)
        if not callable(getattr(client, "call_action", None)):
            raise QQAdapterError("aiocqhttp 平台没有可用的 OneBot client")
        return client

    async def _call(self, action: str, **kwargs: Any) -> Any:
        try:
            result = await self._client().call_action(action=action, **kwargs)
        except QQAdapterError:
            raise
        except Exception as exc:
            raise QQAdapterError(f"OneBot 调用失败（{action}）：{exc}") from exc
        return result

    @staticmethod
    def _qq_id(value: str) -> int | str:
        text = str(value).strip()
        return int(text) if text.isdigit() else text

    async def get_group_info(self, group_id: str) -> dict[str, Any]:
        result = await self._call("get_group_info", group_id=self._qq_id(group_id))
        if not isinstance(result, dict):
            raise QQAdapterError("OneBot 返回的群信息格式不可识别")
        return result

    async def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        result = await self._call(
            "get_group_member_list",
            group_id=self._qq_id(group_id),
        )
        if not isinstance(result, list):
            raise QQAdapterError("OneBot 返回的群成员列表格式不可识别")
        return [member for member in result if isinstance(member, dict)]

    async def send_group_message(self, group_id: str, message: list[dict[str, Any]]) -> Any:
        return await self._call(
            "send_group_msg",
            group_id=self._qq_id(group_id),
            message=message,
        )

    async def send_group_text(self, group_id: str, text: str) -> Any:
        return await self.send_group_message(
            group_id,
            [{"type": "text", "data": {"text": str(text)}}],
        )

    async def send_group_at_all(self, group_id: str, text: str = "") -> Any:
        message: list[dict[str, Any]] = [
            {"type": "at", "data": {"qq": "all"}},
        ]
        if str(text).strip():
            message.append(
                {"type": "text", "data": {"text": f" {str(text).strip()}"}},
            )
        return await self.send_group_message(group_id, message)

    async def send_private_message(self, user_id: str, text: str) -> Any:
        return await self._call(
            "send_private_msg",
            user_id=self._qq_id(user_id),
            message=[{"type": "text", "data": {"text": str(text)}}],
        )

    async def upload_private_file(self, user_id: str, path: Path) -> Any:
        path = Path(path)
        try:
            if path.stat().st_size <= INLINE_FILE_MAX_BYTES:
                file_value = f"base64://{base64.b64encode(path.read_bytes()).decode('ascii')}"
            else:
                file_value = str(path)
        except OSError as exc:
            raise QQAdapterError(f"读取待发送文件失败：{exc}") from exc
        return await self._call(
            "upload_private_file",
            user_id=self._qq_id(user_id),
            file=file_value,
            name=path.name,
        )
