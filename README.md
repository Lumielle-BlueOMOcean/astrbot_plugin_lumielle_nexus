# 微光·群枢 / Lumielle Nexus

微光·群枢（Lumielle Nexus）是一个面向 AstrBot 的跨会话群任务编排插件，让私聊成为控制台，让群聊成为可调度的工作空间。

当前版本：`0.5.0`

## 项目定位

插件面向 QQ + OneBot v11，首轮正式支持 AstrBot 的 `aiocqhttp` 平台，主要适配 NapCat。operator 通过私聊绑定目标群、创建提醒或收集任务，也可以在授权范围内归档和整理已保存的群文本。

## 当前支持

- 私聊绑定群别名、查看群和任务。
- 持久化单次提醒、DDL 提前提醒、每周周期提醒和课程提醒。
- 在群内启动信息收集，支持字段解析、重复提交更新、进度查询和 XLSX 导出。
- 收集结束后尝试通过 QQ 私聊回传 Excel；回传失败不会丢失导出文件。
- 消息归档、关键词/时间范围查询、按需群聊总结。
- 每周自动总结，并将结果私聊发给创建者。
- 跨群转述采用 prepare → preview → explicit confirm → send 流程。
- 按群隔离的成员搜索和成员集合，可用于定向收集和 Relay @成员集合。
- 默认关闭的单成员 mute、unmute、kick，采用独立 moderator 权限和确认预览。
- 可显式开启、带安全校验的 Collection 自然语言填写。

## 安装

将本仓库目录放入 AstrBot 插件目录并重载插件，安装依赖：

```bash
pip install -r requirements.txt
```

需要 AstrBot `>=4.28.0,<5`、`aiocqhttp`/OneBot v11 平台和可用的 AstrBot LLM Provider。运行数据由 AstrBot plugin data directory 管理，默认位于 `data/plugin_data/astrbot_plugin_lumielle_nexus/`，不会写入仓库。

## 配置

- `operator_ids`：允许控制插件的额外 QQ 用户 ID；AstrBot Admin 始终允许。
- `timezone`：默认 `Asia/Shanghai`。
- `scheduler_interval_seconds`：默认 15 秒，运行时限制在 5–60 秒。
- `max_retry_count`：群提醒失败后的最大重试次数，默认 3。
- `collection_ack`：是否确认群成员提交，默认开启。
- `archive_max_message_chars`：单条归档文本最大 4000 字符，运行时限制在 256–20000。
- `archive_retention_days`：默认保留 90 天；设为 0 表示不自动清理，其他值限制在 7–3650 天。
- `moderation_enabled`：默认 `false`；必须主动开启才允许群管理。
- `moderator_ids`：独立的群管理 QQ 用户 ID 列表，不会自动继承 `operator_ids`。

普通群枢控制型命令和工具要求来自私聊，并由 AstrBot Admin 或 `operator_ids` 授权；群管理操作另受独立 moderator 权限域约束。

## 群绑定与提醒

先在私聊中绑定群：

```text
/nexus bind 班群 123456789
/nexus groups
```

之后可以直接私聊：

```text
明天下午三点在班群提醒所有人交实验报告。
```

也可以使用可靠的命令 fallback：

```text
/nexus tasks
/nexus task R-...
/nexus cancel R-...
```

DDL、周期、课程和 collection chase 都会持久化，插件重载后由同一个 scheduler 恢复。停机期间错过过久的周期/课程/DDL 提前提醒会跳过，普通单次提醒仍保持 durable 行为。

发送队列采用持久化的 at-least-once 执行语义；需要避免重复外部操作的 relay 则不进入自动重试队列。

## 数据收集与 Excel

例如私聊：

```text
在班群统计国庆离校信息，需要姓名、离校时间、返校时间，现在开始。
```

群成员按以下格式提交即可：

```text
姓名：张三
离校时间：10月1日 14:00
返校时间：10月6日
```

同一成员再次提交会更新当前有效记录。可使用 `/nexus collect-start`、`/nexus collect-status`、`/nexus collect-stop` 作为 fallback。结束时生成包含统计结果、未提交成员和任务信息的 XLSX，并保存到 plugin data directory 的 `exports/`。

## 自然语言信息收集

自然语言填写默认关闭。创建 Collection 时必须由 operator 明确开启 `ai_extraction`；开启后也只有以 `提交：`、`填报：`、`报名：` 或 `更新：`、`修改：`、`更正：` 开头的群消息才会进入识别流程，普通群聊不会发送给 LLM。标准的 `字段：值` 格式始终优先且不消耗 LLM。

自然语言提交使用创建 Collection 时绑定的 AstrBot LLM Provider，只保存 Provider ID，不保存 API key、token 或 credential。模型只返回候选 JSON，插件会检查字段白名单、原文 evidence、confidence、值长度和重复歧义后才写入；低置信度或冲突字段不会写库。成功后会 echo 实际写入的数据供成员检查，AI 解析可能出错，建议优先使用标准格式。

例如创建时明确允许后，成员可以发送：

```text
提交：我是张三，10月3日下午离校，7号晚上返校
```

需要修改已有内容时使用：

```text
更正：返校时间改为10月8日晚上
```

`提交`/`填报`/`报名` 只填充当前为空的字段，不会覆盖已有值；`更新`/`修改`/`更正` 才允许可靠识别后覆盖，并会在识别期间记录发生变化时拒绝旧结果。

## 成员集合

成员集合按 QQ 群隔离，例如可以在班群建立“班委”或“实验 A 组”。成员只能通过实时群成员列表中的 QQ 号、精确群名片或精确昵称加入；同名时不会猜测。信息收集可以在创建时 snapshot 一个集合，之后名单变化不会影响这次收集；Relay 也可以在 preview 时 snapshot 当前仍在群内的集合成员并在确认时 @这些确定的 QQ 号。

已退群成员不会从保存的名单历史中自动删除；新增或修改集合不会回写既有任务快照。

## 群消息归档

归档默认关闭。绑定群不会自动开始记录；必须显式开启：

```text
/nexus archive 班群 on
/nexus archive-status 班群
```

开启后才记录新的文本消息，不会回溯开启前的历史；图片、语音、文件和视频内容不会解析。归档默认保留 90 天，也可以通过 `archive_retention_days` 调整或使用 `nexus_clear_archive` 显式清除已有文本数据。关闭归档只停止新增，不会自动删除已有记录。

## 群聊总结

可以按时间范围请求总结：

```text
总结一下班群最近一周发生了什么。
帮我整理班群最近一周所有作业、DDL 和需要班委处理的事情。
```

总结使用 AstrBot 当前 LLM Provider，长记录会按消息边界分块，最多处理最近 2000 条文本消息。消息数和活跃成员数由插件确定性统计。模型只负责整理，不会根据总结自动创建 DDL 或其他任务；DDL/待办内容仍然只是候选，需要 operator 判断后再确认创建。

也可以创建自动周报：

```text
每周日晚十点把班群本周总结私聊发给我。
```

周报要求目标群已开启归档；归档关闭期间到期的周报会跳过，但不会取消后续计划，重新开启后下一次 occurrence 会恢复。周报按照逻辑计划时间回看固定窗口，停机恢复不会积累大量旧周报；LLM 已生成但 QQ 私聊发送失败时，会复用缓存结果和已发送分片进度重试发送。总结有输入预算和最多 10 个分片调用加 1 次合并调用，群聊记录会作为不可信数据处理。

## 跨群转述

跨群消息不是一次自然语言请求就直接发送。流程固定为：

```text
prepare → preview → explicit confirm → send
```

例如先说“把这段发到班委群”，插件只创建待确认 preview 和 `X-...` Relay ID；用户明确确认后，才调用 `nexus_confirm_relay` 或：

```text
/nexus relay-confirm X-...
```

preview 默认 1 小时有效，且只能由创建 preview 的 operator 确认。失败的 relay 不会自动重试，避免重复发送；可以重新准备。如果确认发送过程中进程异常退出，Relay 会标为 FAILED，发送结果未知，不会自动重发；请先查看目标群后再决定是否重新准备。

成员集合 Relay 会按每批最多 20 人发送，首次成功批次携带正文，后续批次只发送 @。每批成功后都会持久化已 @ 的 QQ 号；若发送失败，结果会标记为部分成功并且不会自动重试。进程在 QQ 已成功但进度尚未落库的极小窗口仍可能产生重复 mention，系统不宣称 exactly-once。

## 群管理

群管理默认完全关闭。开启 `moderation_enabled` 后，仍必须由 AstrBot Admin 或单独配置在 `moderator_ids` 的用户从私聊发起；普通 `operator_ids` 不会隐式升级。当前只支持一次针对一个群、一个成员的 mute、unmute、kick，不支持批量管理、全员禁言、设置管理员、退群或自动管理。

群管理固定采用 `prepare → preview → explicit confirm → execute`，preview 有 10 分钟 TTL，确认前会重新检查 Bot 与目标成员角色；缺失或未知角色一律拒绝，权限变化会阻止调用 OneBot。群管理不自动重试，进程在确认执行中断时会记录为 outcome unknown，需要人工检查群状态。MODERATION 任务属于独立 moderator 权限域：普通 operator 无权查看详情、确认或取消；创建该操作的 moderator 只能处理自己的 preview，AstrBot Admin 可作为覆盖者取消或查看。

## 当前限制与后续规划

当前归档只保存可读文本表现，不包含 OCR、语音转写、图片下载、向量检索或长期消息语义抽取。也不包含 WebUI、Redis、ORM、RAG、批量群管理或自动根据总结创建任务。

后续可考虑：recurring schedule 增强、DDL 工作流、课程表、未提交成员自动催办、weekly summary 增强、message archive 扩展、relay 工作流、成员集合增强，以及更丰富但仍需确认的 moderation 策略。
