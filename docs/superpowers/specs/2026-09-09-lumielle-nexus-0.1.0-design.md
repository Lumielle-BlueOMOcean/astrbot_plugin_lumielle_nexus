# Lumielle Nexus 0.1.0 Design

## Goal

建立一个面向 AstrBot 4.28.0、正式支持 `aiocqhttp` 的 QQ 群任务插件，让 operator 通过私聊绑定群、创建一次性提醒或信息收集任务，并完成“私聊创建 → 群内执行 → SQLite 持久化 → 私聊查询/结束 → XLSX 导出”的首条完整链路。

## Chosen approach

采用与插件入口分离的五文件轻量结构：`main.py` 只连接 AstrBot，`core.py` 负责任务生命周期，`storage.py` 负责 SQLite，`qq_adapter.py` 负责 OneBot 能力，`exporter.py` 负责 XLSX。Storage 使用标准库 `sqlite3`，TaskManager 以 `asyncio.Lock` 串行化会改变状态的业务操作；不增加 ORM、Redis、HTTP server 或复杂 scheduler。

AstrBot 接入使用 `Star` 子类自动注册、`@filter.llm_tool`、`@filter.command_group`/子命令、`@filter.event_message_type` 和 `AstrMessageEvent` 的公开方法。插件数据目录使用 4.28.0 的 `StarTools.get_data_dir("astrbot_plugin_lumielle_nexus")`，数据库和 exports 位于 AstrBot `data/plugin_data` 下，绝不写回仓库。

## Data and lifecycle

- `group_bindings` 以 `(platform_id, group_id)` 唯一，保存 operator 提供的 alias 和创建者。
- `tasks` 保存 `REMINDER` 或 `COLLECTION` 及 `PENDING`、`PROCESSING`、`ACTIVE`、`COMPLETED`、`CANCELLED`、`FAILED` 状态。payload/result 使用 JSON，task id 为 `R-YYYYMMDD-NNN` 或 `C-YYYYMMDD-NNN`。
- `collection_entries` 以 `(task_id, sender_id)` 唯一，重复提交通过 upsert 更新同一条最终记录，并保留 partial parsed data。
- 启动时创建单一 scheduler task；到期提醒先原子领取，再发送群消息，发送成功完成，失败记录并按配置重试。terminate 取消 scheduler 并关闭 SQLite。
- 同一 platform 和 group 同时最多一个 active collection；停止收集先在锁内切换为 processing，随后生成导出，避免新提交破坏结束状态。

## Authorization and QQ integration

所有控制型命令和 LLM tools 统一要求事件为私聊，且 `event.is_admin()` 或 sender id 在配置 `operator_ids`。LLM tool 内部重复检查，不依赖模型自律。群消息 listener 不要求成员有权限，只处理绑定群当前 active collection，忽略 bot 自身消息。

`QQAdapter` 只支持 `aiocqhttp`，普通回复使用事件发送或 AstrBot `StarTools.send_message_by_id`；群信息、成员列表、@全体和私聊文件上传统一通过平台 client 的 `call_action()`。OneBot 失败转换为插件可读错误，成员列表失败只影响统计补全，不影响任务或导出。

## Collection behavior

启动时向群发送标题、字段和填写说明，可选 `@全体`。解析器支持中英文冒号和字段乱序；即使只有一个字段也要求使用 `字段：值` 或 `字段: 值` 格式，普通聊天不会被记录；缺少字段仍保存 partial entry 并回复缺失字段（可配置关闭确认）。状态查询返回有效群成员提交数、尽力获取的群成员数、未提交数和状态。停止后生成三个 sheet：`统计结果`、`未提交成员`、`任务信息`，并尽力通过 OneBot `upload_private_file` 回传创建者；回传失败不回滚导出或任务结果。

## Verification

核心 smoke suite 使用 `unittest` 覆盖 schema、绑定 CRUD、reminder create/list/cancel、collection create/upsert 和 XLSX export。最终集中执行 `python3 -m compileall .`、可用的 `ruff check .`、核心 smoke，以及在本机 AstrBot 4.25.5 源码路径下进行插件 import/load 级检查。4.28.0 API 以官方源码/文档为准；真实 QQ/NapCat 行为只记录为未连接验证项。

## Explicit non-goals

本版不实现 WebUI、群历史归档、周总结、RAG、多 Agent、kick、mute、whole-ban、群管理、复杂周期课表、复杂 RRULE、自由文本 LLM 抽取或 OCR。
