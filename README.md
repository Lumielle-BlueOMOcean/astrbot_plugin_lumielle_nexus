# 微光·群枢 / Lumielle Nexus

微光·群枢（Lumielle Nexus）是一个面向 AstrBot 的跨会话群任务编排插件，让私聊成为控制台，让群聊成为可调度的工作空间。

当前版本：`0.2.0`

## 项目定位

插件面向 QQ + OneBot v11，正式支持 AstrBot 的 `aiocqhttp` 平台，主要用于 NapCat 场景。operator 通过私聊绑定目标群、创建时间任务或信息收集任务，群内成员直接提交，operator 再私聊查看进度或结束统计。

## 当前支持

- 私聊绑定、查看和管理群别名。
- 持久化的一次性定时提醒，支持可选 @全体和失败重试。
- 每周 weekday/time 周期提醒。
- DDL 截止时间和多个提前提醒。
- 每周课程提醒，支持地点、提前分钟数和日期范围。
- 私聊自然语言可调用的群枢 LLM tools。
- 群内字段式信息收集，同一成员重复提交会更新当前有效记录。
- 针对信息收集未提交成员的单次或重复催办，只 @当前未提交成员。
- 收集进度查询、停止收集、XLSX 导出和 QQ 私聊文件回传尝试。
- SQLite 持久化、重载恢复和提醒失败的 30/120/300 秒退避重试。
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
/nexus task D-20260909-001
/nexus cancel R-20260909-001  # 取消尚未执行的一次性提醒，或取消可取消的父任务
```

收集命令：

```text
/nexus collect-start 班群|国庆离校信息|姓名,离校时间,返校时间|请按格式填写|all
/nexus collect-status C-20260909-001
/nexus collect-stop C-20260909-001
```

`/nexus help` 会显示完整 fallback 用法。命令主要用于初始化、调试和模型工具无法正确调用时的确定性操作。`/nexus cancel` 也可以取消 ACTIVE 的 DDL、周期提醒或课程父任务，并级联取消尚未执行的内部提醒；Collection 必须使用 `/nexus collect-stop`。

## 私聊自然语言示例

绑定群后，可以直接私聊：

```text
明天下午三点在班群提醒所有人交实验报告。
```

或：

```text
在班群统计国庆离校信息，需要姓名、离校时间、返校时间，艾特全体开始。
```

其他自然语言示例：

```text
高数作业 9 月 15 日 23:59 截止，提前三天、一天和三小时提醒班群。
每周一三五早上 7:40 在班群提醒大家打卡。
以后每周二 10:00 高等数学，A101，上课前 20 分钟提醒班群，到 1 月 15 日结束。
每天晚上 8 点催还没填离校信息的人，直到统计结束。
```

提醒时间由模型转换为明确的 `YYYY-MM-DD HH:MM` 后交给工具，并按配置时区解析。

工具中的 `weekdays` 使用 `1=Monday` 到 `7=Sunday`；DDL 的 `remind_before_minutes` 单位是分钟。周期规则首版只支持每周星期/时间，不支持任意 cron、RRULE、农历、节假日历或自动课表导入。

## 数据收集与 Excel

启动收集后，插件会向群内发送标题、字段和填写说明。成员可以发送：

```text
姓名：张三
离校时间：10月1日 14:00
返校时间：10月6日
```

支持中文或英文冒号、字段乱序；即使只有一个字段，也必须使用 `字段：值` 或 `字段: 值` 格式，普通聊天不会被记录。停止任务后，会在 plugin data 的 `exports/` 生成包含 `统计结果`、`未提交成员`、`任务信息` 三个 sheet 的 `.xlsx`，并尝试通过 QQ 私聊回传文件。如果 OneBot 无法取得完整群成员名单，导出仍会保留，但未提交人数无法准确计算。QQ 文件回传失败也不会丢失导出文件，回复只展示文件名，不展示服务器绝对路径。

`/nexus cancel` 可以取消尚未执行的一次性提醒，也可以取消 ACTIVE 的 DDL、周期提醒或课程父任务，并级联取消尚未执行的内部提醒；进行中的信息收集请使用 `/nexus collect-stop <任务ID>`，以便正常生成统计结果。

提醒发送采用持久化队列和至少一次（at-least-once）投递语义：在消息已经发出但进程尚未来得及写入完成状态时发生崩溃，重载后可能再次发送同一提醒。

## 时间任务说明

DDL 会创建一个逻辑父任务，并为仍在未来的提前时间生成内部一次性提醒；例如 `4320、1440、180` 分钟分别代表提前 3 天、1 天和 3 小时。过去的提前时间会跳过，截止时间必须在未来。

周期提醒和课程提醒只保留下一次 occurrence。Bot 停机期间错过的旧 occurrence 默认跳过，恢复后不补发陈旧的上课或打卡消息；刚错过且在约 120 秒宽限期内的 occurrence 仍可执行。内部子提醒默认不会出现在普通任务列表中。

信息收集催办会在执行时重新获取群成员，只 @有效且尚未提交的成员；没有未提交成员时不发送消息，重复催办最短间隔为 60 分钟，统计结束后后续催办会自动跳过。

## 当前限制与后续规划

当前版本仍有这些限制：同一群同一时间最多一个 active collection；周期规则只支持 weekly weekday/time；Collection 提交必须使用字段冒号格式，暂不提供复杂自然语言成员信息抽取。

后续规划包括自然语言 collection extraction、weekly summary、message archive、cross-group relay、member sets，以及 mute/kick/moderation。上述能力当前均未实现。
