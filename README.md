# 微光·群枢 / Lumielle Nexus

微光·群枢（Lumielle Nexus）是一个面向 AstrBot 的跨会话群任务编排插件，让私聊成为控制台，让群聊成为可调度的工作空间。

## 项目定位

插件首版面向 QQ + OneBot v11，正式支持 AstrBot 的 `aiocqhttp` 平台，主要用于 NapCat 场景。operator 通过私聊绑定目标群、创建一次性提醒或信息收集任务，群内成员直接提交，operator 再私聊查看进度或结束统计。

## 当前支持

- 私聊绑定、查看和管理群别名。
- 持久化的一次性定时提醒，支持可选 @全体和失败重试。
- 私聊自然语言可调用的群枢 LLM tools。
- 群内字段式信息收集，同一成员重复提交会更新当前有效记录。
- 收集进度查询、停止收集、XLSX 导出和 QQ 私聊文件回传尝试。
- SQLite 数据保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_lumielle_nexus/` 下。

## 安装

将仓库目录放到 AstrBot 的插件目录，或通过 AstrBot 插件管理器安装。安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

需要 AstrBot `>=4.28.0,<5`，并配置可用的 `aiocqhttp` OneBot v11 连接（QQ/NapCat）。本插件不会修改 AstrBot Core。

## 配置

在 AstrBot 插件配置中填写：

- `operator_ids`：额外允许控制插件的 QQ 用户 ID；AstrBot Admin 始终允许。
- `timezone`：默认 `Asia/Shanghai`。
- `scheduler_interval_seconds`：默认 15 秒，运行时限制在 5–3600 秒。
- `max_retry_count`：提醒发送失败的最大重试次数，默认 3。
- `collection_ack`：是否确认群成员提交，默认开启。

所有控制操作都必须来自私聊，并由 AstrBot Admin 或 `operator_ids` 授权。群成员提交收集信息不需要 operator 权限。

## 群绑定与命令 fallback

```text
/nexus bind 班群 123456789
/nexus groups
/nexus tasks
/nexus cancel R-20260909-001  # 仅取消尚未执行的一次性提醒
```

收集命令：

```text
/nexus collect-start 班群|国庆离校信息|姓名,离校时间,返校时间|请按格式填写|all
/nexus collect-status C-20260909-001
/nexus collect-stop C-20260909-001
```

`/nexus help` 会显示完整 fallback 用法。命令主要用于初始化、调试和模型工具无法正确调用时的确定性操作。

## 私聊自然语言示例

绑定群后，可以直接私聊：

```text
明天下午三点在班群提醒所有人交实验报告。
```

或：

```text
在班群统计国庆离校信息，需要姓名、离校时间、返校时间，艾特全体开始。
```

提醒时间由模型转换为明确的 `YYYY-MM-DD HH:MM` 后交给工具，并按配置时区解析。

## 数据收集与 Excel

启动收集后，插件会向群内发送标题、字段和填写说明。成员可以发送：

```text
姓名：张三
离校时间：10月1日 14:00
返校时间：10月6日
```

支持中文或英文冒号、字段乱序；即使只有一个字段，也必须使用 `字段：值` 或 `字段: 值` 格式，普通聊天不会被记录。停止任务后，会在 plugin data 的 `exports/` 生成包含 `统计结果`、`未提交成员`、`任务信息` 三个 sheet 的 `.xlsx`，并尝试通过 QQ 私聊回传文件。如果 OneBot 无法取得完整群成员名单，导出仍会保留，但未提交人数无法准确计算。QQ 文件回传失败也不会丢失导出文件，回复只展示文件名，不展示服务器绝对路径。

`/nexus cancel` 只用于取消尚未执行的一次性提醒；进行中的信息收集请使用 `/nexus collect-stop <任务ID>`，以便正常生成统计结果。

提醒发送采用持久化队列和至少一次（at-least-once）投递语义：在消息已经发出但进程尚未来得及写入完成状态时发生崩溃，重载后可能再次发送同一提醒。

## 当前限制与后续规划

当前版本是单次任务基础版：同一群同一时间最多一个 active collection；暂不提供复杂自然语言成员信息抽取。

后续规划包括 recurring schedule、DDL task、course schedule、自然语言 collection extraction、未提交成员自动催办、weekly summary、message archive、relay、member sets，以及 mute/kick/moderation。上述能力当前均未实现。
