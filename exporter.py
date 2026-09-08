"""XLSX export for completed collection tasks."""

from __future__ import annotations

import json
import re
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook


def _safe_filename(text: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text or "")).strip(" .")
    return (cleaned or fallback)[:80]


def _display_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return str(value)


def export_collection(
    export_dir: Path,
    task: dict[str, Any],
    entries: list[dict[str, Any]],
    members: list[dict[str, Any]] | None = None,
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
    result_sheet.append(
        ["QQ", "群昵称", *fields, "提交时间", "最后更新时间", "原始提交"],
    )
    for entry in entries:
        parsed = json.loads(entry.get("parsed_data") or "{}")
        result_sheet.append(
            [
                entry.get("sender_id", ""),
                entry.get("sender_name", ""),
                *[parsed.get(field, "") for field in fields],
                _display_time(entry.get("submitted_at")),
                _display_time(entry.get("updated_at")),
                entry.get("raw_message", ""),
            ],
        )

    missing_sheet = workbook.create_sheet("未提交成员")
    if members is None:
        missing_sheet.append(["无法获取完整群成员名单，未提交人数不可准确计算。"])
    else:
        missing_sheet.append(["QQ", "群昵称"])
        submitted_ids = {str(entry.get("sender_id", "")) for entry in entries}
        for member in members:
            member_id = str(member.get("user_id", ""))
            if member_id and member_id not in submitted_ids:
                missing_sheet.append(
                    [member_id, member.get("card") or member.get("nickname") or ""],
                )

    info_sheet = workbook.create_sheet("任务信息")
    info_sheet.append(["项目", "值"])
    info_rows = [
        ("任务 ID", task.get("id", "")),
        ("标题", title),
        ("群", task.get("group_alias") or task.get("group_id", "")),
        ("群号", task.get("group_id", "")),
        ("创建者", task.get("creator_id", "")),
        ("开始时间", _display_time(task.get("created_at"))),
        ("结束时间", _display_time(task.get("finished_at"))),
        ("字段", "、".join(fields)),
        ("总提交人数", len(entries)),
    ]
    for row in info_rows:
        info_sheet.append(list(row))

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            font = copy(cell.font)
            font.bold = True
            cell.font = font
    workbook.save(output_path)
    return output_path
