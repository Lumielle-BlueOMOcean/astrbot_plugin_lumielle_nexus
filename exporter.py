"""XLSX export for completed collection tasks."""

from __future__ import annotations

import json
import re
from copy import copy
from pathlib import Path
from typing import Any

from openpyxl import Workbook

if __package__:
    from .core import collection_member_stats, format_local_time
else:
    from core import collection_member_stats, format_local_time

_ILLEGAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _safe_filename(text: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text or "")).strip(" .")
    return (cleaned or fallback)[:80]


def _safe_cell_value(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _ILLEGAL_CONTROL_RE.sub("", text)
    if text.lstrip(" \t\r\n").startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _safe_row(values: list[Any]) -> list[str]:
    return [_safe_cell_value(value) for value in values]


def export_collection(
    export_dir: Path,
    task: dict[str, Any],
    entries: list[dict[str, Any]],
    members: list[dict[str, Any]] | None = None,
    *,
    self_id: str | None = None,
    target_ids: list[str] | set[str] | None = None,
    timezone_name: str = "UTC",
) -> Path:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(task.get("payload") or "{}")
    fields = [str(field) for field in payload.get("fields", [])]
    title = payload.get("title") or task.get("group_alias") or task["id"]
    output_path = export_dir / f"{_safe_filename(title, task['id'])}_{task['id']}.xlsx"

    workbook = Workbook()
    result_sheet = workbook.active
    result_sheet.title = "统计结果"
    result_sheet.append(_safe_row(["QQ", "群昵称", *fields, "提交时间", "最后更新时间", "原始提交"]))
    for entry in entries:
        parsed = json.loads(entry.get("parsed_data") or "{}")
        result_sheet.append(_safe_row([
                entry.get("sender_id", ""),
                entry.get("sender_name", ""),
                *[parsed.get(field, "") for field in fields],
                format_local_time(entry.get("submitted_at"), timezone_name),
                format_local_time(entry.get("updated_at"), timezone_name),
                entry.get("raw_message", ""),
            ]))

    missing_sheet = workbook.create_sheet("未提交成员")
    if members is None:
        missing_sheet.append(_safe_row(["无法获取完整群成员名单，未提交人数不可准确计算。"]))
    else:
        missing_sheet.append(_safe_row(["QQ", "群昵称"]))
        stats = collection_member_stats(
            members, entries, self_id=self_id, target_ids=target_ids,
        )
        for member_id in sorted(stats["missing_ids"]):
            member = stats["eligible_members"][member_id]
            missing_sheet.append(_safe_row([
                member_id,
                member.get("card") or member.get("nickname") or "",
            ]))

    info_sheet = workbook.create_sheet("任务信息")
    info_sheet.append(_safe_row(["项目", "值"]))
    info_rows = [
        ("任务 ID", task.get("id", "")),
        ("标题", title),
        ("群", task.get("group_alias") or task.get("group_id", "")),
        ("群号", task.get("group_id", "")),
        ("创建者", task.get("creator_id", "")),
        ("开始时间", format_local_time(task.get("created_at"), timezone_name)),
        ("结束时间", format_local_time(task.get("finished_at"), timezone_name)),
        ("字段", "、".join(fields)),
        ("总提交人数", len(entries)),
    ]
    if payload.get("target_member_set"):
        info_rows.extend([
            ("目标成员集合", payload["target_member_set"]),
            ("目标快照人数", len(payload.get("target_member_ids") or [])),
        ])
    for row in info_rows:
        info_sheet.append(_safe_row(list(row)))

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            font = copy(cell.font)
            font.bold = True
            cell.font = font
    workbook.save(output_path)
    return output_path
