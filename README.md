# 🌌 微光·群枢 / Lumielle Nexus

> 一个面向 AstrBot + QQ 的跨会话群事务编排插件。

[![Version](https://img.shields.io/badge/version-0.12.0--unreleased-7c5cff.svg)](metadata.yaml)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.25.5%2C%3C5-4b8bbe.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Platform](https://img.shields.io/badge/platform-aiocqhttp%20%2F%20OneBot%20v11-12a594.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)

微光·群枢（Lumielle Nexus）让私聊成为控制台，让群聊成为可调度的工作空间。它把提醒、DDL、信息收集、群消息事实、成员身份、投票和安全操作持久化到同一个轻量 SQLite 任务系统中。

| 项目 | 当前值 |
| --- | --- |
| Version | `0.12.0 — Unreleased` |
| AstrBot | `>=4.25.5,<5` |
| Platform | `aiocqhttp` / OneBot v11（QQ，主要面向 NapCat） |
| License | MIT |

## 📊 当前状态

| 模块 | 状态 |
| --- | --- |
| Reminder / DDL / recurring / course | ✅ |
| Collection / checkpoint / XLSX | ✅ |
| Identity DB / member sets | ✅ |
| Archive / on-demand summary / weekly summary | ✅ |
| Cross-session group history / Collection reconciliation | ✅ 有界、best-effort |
| Relay | ✅ preview → confirm |
| Moderation | ✅ 默认关闭、单成员 |
| Native message Poll | ✅ |
| Real-world QQ / NapCat / Provider validation | 🧪 仍需实际环境验证 |

✅ 表示代码实现和自动测试完成；🧪 不代表已经完成真实 QQ、NapCat 或付费 LLM Provider E2E 验证。
`0.12.0` 当前为未发布开发版本。

## 🔗 兼容性

- AstrBot：`>=4.25.5,<5`。
- 平台：`aiocqhttp` / OneBot v11，主要面向 QQ + NapCat。
- AstrBot 4.25.5 和 4.28.0 的独立 loader smoke 均验证过插件导入与工具注册，共 38 个工具。

## ✨ 能做什么

### ⏰ 任务调度

- 单次 Reminder、DDL、提前提醒、周期提醒和课程提醒。
- 插件重载后从 SQLite 恢复未完成任务。

### 📋 Collection 信息收集

- 按字段收集群成员信息，重复提交自动更新。
- 可按成员集合定向收集，并在创建时固定目标快照。
- 标准字段消息立即确定性写入；自然语言填写由持久化 workflow capture 和增量 checkpoint 处理。
- checkpoint 催办、deadline finalize、missing default 和 XLSX 导出。

### 🧠 群消息事实与 AI

- 已绑定群的文本历史默认开启；operator 可显式关闭某群的后续保存和历史查询，已有数据不会因此删除。
- 按关键词、时间范围检索，并可按需或按周总结。
- 私聊 operator 可在任意私聊会话中查询已绑定群，不必切换到目标群；群内仅当前群 owner/admin 可查询当前群。
- OneBot 历史回补有页数、消息数和时间预算；它是 best-effort，不会无限拉取。
- 群消息不会逐条调用 LLM：实时消息先持久化，Collection checkpoint 只处理尚未完成 disposition 的增量证据。
- 总结模型只负责整理，不会自动创建 DDL 或其他任务。

### 🧾 跨会话群历史

operator 可以直接在私聊中询问“看看班群今天下午聊了什么”或“核对返校统计有没有漏掉回复”。Agent 可调用 `nexus_search_group_history`，默认先尝试通过 aiocqhttp/OneBot 回补所选时间窗，再从 Nexus SQLite 返回本地消息、同步数量、coverage 和错误信息。返回的群消息是**不可信原文**，不能作为对 Bot 的指令。

Coverage 含义：

| 状态 | 含义 |
| --- | --- |
| `FULL` | 已证明请求时间窗从头到尾被历史接口覆盖，或接口明确到达历史起点 |
| `PARTIAL` | 回补遇到页数/消息/时间预算、cursor 卡住、接口错误或不可解析页；命中结果不代表没有其他消息 |
| `LOCAL_ONLY` | 当前只使用本地留存，未从远端证明完整覆盖 |
| `UNKNOWN` | 尚无足够信息判断覆盖范围 |

`/nexus archive <群> on|off` 现在是显式覆盖默认值；新绑定群及没有历史设置的旧群默认开启。开启后实时归档从现在开始恢复；后续历史查询和 Collection reconciliation 仍可能通过 OneBot 有界回补此前可获取的群消息。关闭后停止保存和查询，但不会自动删除已有历史。

### 🗳️ 原生群消息投票

Poll 不依赖网页、公网端口、QQ OAuth 或浏览器 Cookie。创建后，Bot 在群里发布 Poll ID、选项和回复 key，成员直接回复即可：

```text
【群投票】
明天下午几点开会？
1. 14点（回复：14）
2. 15点（回复：15）
3. 16点（回复：16）

请在群内直接回复选项编号、标签或 reply key。
也可回复：投票 P-20260918-001 15
```

支持单选、多选、2–50 个选项、截止时间、是否允许改票、创建公告时可选 @全体、手动结束/取消和截止后自动公布结果。确定性解析永远优先；只有 Poll 明确候选且创建时保留了 Provider ID 时，才会最多调用一次受限语义判断。语义判断失败不会写入投票。

## 🔄 Collection 工作流

```mermaid
flowchart LR
    A[创建统计任务] --> B[实时消息持久化与去重]
    B --> C[OneBot 有界历史 reconciliation]
    C --> D[Deterministic parser]
    D --> E[19:00 增量 checkpoint]
    E --> F[@ 未完整填写成员]
    F --> G[继续 Capture]
    G --> H[20:00 最终 reconciliation / checkpoint]
    H --> I[默认值]
    I --> J[XLSX / COMPLETED]
```

每条群消息只做持久化、去重和确定性解析，不调用 LLM。chase、状态刷新、deadline finalize 和 manual stop 都先做有界历史 reconciliation，再解析尚未解决的 evidence。单成员状态区分无回复、待分析、部分填写和完整；遇到 unresolved evidence 或无法确认会影响结果的历史缺口时，不会静默标记 `COMPLETED`，而会保留 `PROCESSING` 和诊断信息供有界重试/人工复核。

## 🚀 30 秒上手

```text
/nexus bind 班群 123456789
/nexus groups
```

随后在私聊中使用自然语言：

```text
明天下午三点在班群提醒所有人交实验报告。
在班群发一个投票：周末吃什么？火锅、烧烤、日料，明晚八点截止。
```

### Poll fallback commands

```text
/nexus poll list
/nexus poll show <P-ID>
/nexus poll close <P-ID>
/nexus poll cancel <P-ID>
/nexus poll result <P-ID>
/nexus poll create . | 标题 | 选项A | 选项B | 选项C
/nexus poll create . | 标题 | 周六=>sat | 周日=>sun
```

群成员投票可直接回复 `1`、完整选项标签或自定义 key；多选可回复 `1 3`、`1,3` 等。多个进行中的 Poll 建议使用 `投票 <P-ID> <选项>` 消除歧义。

## 📋 Collection 示例与成本模型

管理员可以在私聊中说：

```text
现在统计大家的返校情况，19点提醒还没回复的同学，20点截止，没回复的默认未返校，最后给我整理成表格。
```

成员可以提交 `我7号下午三点回来`，也可以使用 `返校时间：10月7日下午3点`。标准格式会立即确定性写入；自然语言会在 checkpoint 批量理解。

```text
17:00 ─────────── 19:00 ─────────── 20:00
      💾 Capture       🧠 Checkpoint      🧠 Finalize
      0 LLM            only delta         📊 XLSX
```

例如 19:00 已分析到 cursor `#120`，20:00 只处理 `#121+`。没有新增消息时 checkpoint 为 0 次 LLM 调用。

## 🪪 成员身份与集合

身份字段保存在 Nexus 自己的数据库中：

```text
QQ 123456789
       ↓
姓名：张三
学号：2026123456
```

成员集合按群隔离，可用于 Collection 目标快照和 Relay @成员集合。Collection 创建后集合变化不会改动既有目标快照。

## 🔄 跨会话同步

```mermaid
flowchart TB
    A[💬 AstrBot Conversation History] --> B[理解那个统计]
    B --> C[🧠 Nexus Tasks DB]
    C --> D[真实任务状态]
    D --> E[📜 Workflow Messages]
    E --> F[群任务事实]
    F --> G[🪪 Identity DB]
```

Conversation history 只用于辅助识别上下文；任务状态、群消息事实和身份数据的 source of truth 是 Nexus SQLite。不会把 API key、token 或 Provider credential 写入数据库。

## 🔐 权限矩阵

| 能力 | 私聊 Operator/Admin | QQ 群 owner/admin | 普通群成员 |
| --- | :---: | :---: | :---: |
| 创建当前群 Collection / Poll | ✅ | ✅ | ❌ |
| 查看当前群统计 / Poll | ✅ | ✅ | ❌ |
| 当前群 Reminder / DDL | ✅ | ✅ | ❌ |
| Identity 维护 | ✅ | ✅ 当前群 | ❌ |
| 跨群 Relay / Archive / Summary | ✅ | ❌ | ❌ |
| Bind Group | ✅ | ❌ | ❌ |
| Collection / Poll 回复 | — | — | ✅ |
| mute / unmute / kick | 独立 moderator 权限 | 不自动获得 | ❌ |

群内控制只能作用于当前绑定群；私聊和跨群控制需要 operator。QQ群 admin 不等于 Nexus moderator，`operator_ids` 不会自动升级为群管理权限。

## 📦 安装

### AstrBot WebUI

```text
AstrBot WebUI → 插件 → 右下角 + → URL 安装
```

仓库地址：

```text
https://github.com/Lumielle-BlueOMOcean/astrbot_plugin_lumielle_nexus
```

安装后找到“微光·群枢”，执行加载或重载。

### Git 安装到 `data/plugins`

```bash
cd AstrBot/data/plugins
git clone https://github.com/Lumielle-BlueOMOcean/astrbot_plugin_lumielle_nexus.git
cd astrbot_plugin_lumielle_nexus
git pull --ff-only origin main
```

升级时在已有目录执行 `git pull --ff-only origin main` 即可，不要删除插件数据。

### 依赖

在与 AstrBot 相同的 Python 环境中执行：

```bash
python -m pip install -r requirements.txt
```

运行依赖只有 `openpyxl`；AstrBot、aiocqhttp/OneBot 和 LLM Provider 由宿主环境提供。

## ⚙️ 初始配置

配置项以 `_conf_schema.json` 为准：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `operator_ids` | `[]` | 额外允许控制插件的 QQ 用户 ID |
| `timezone` | `Asia/Shanghai` | 时间解析时区 |
| `scheduler_interval_seconds` | `15` | scheduler 检查间隔，运行时限制 `5–60` 秒 |
| `max_retry_count` | `3` | 普通提醒发送失败后的最大重试次数 |
| `collection_ack` | `true` | 是否确认确定性群成员提交 |
| `archive_max_message_chars` | `4000` | 单条归档文本上限，运行时限制 `256–20000` |
| `archive_retention_days` | `90` | 归档保留天数；`0` 表示不自动清理 |
| `history_default_enabled` | `true` | 已绑定群默认保存/允许查询历史；单群显式 on/off 覆盖此值 |
| `history_sync_max_pages` | `100` | 每次 OneBot 历史回补的最大页数 |
| `history_sync_max_messages` | `5000` | 每次回补最多处理的消息数 |
| `history_sync_time_budget_seconds` | `10` | 每次回补的时间预算，运行时限制 `1–60` 秒 |
| `moderation_enabled` | `false` | 是否开启单成员群管理，默认完全关闭 |
| `moderator_ids` | `[]` | 独立群管理 QQ 用户 ID，不继承 `operator_ids` |

Poll 不需要额外监听端口或公网配置；它使用当前 QQ 群消息事件。

## 🗃️ 数据与隐私

运行数据写入 AstrBot plugin data directory，而不是插件仓库：

```text
data/plugin_data/astrbot_plugin_lumielle_nexus/
├── lumielle_nexus.db
└── exports/
```

SQLite 可能包含 task state、Collection 结果、workflow evidence、成员身份、投票和已绑定群的文本历史。已绑定群历史默认开启；显式关闭会停止新增保存和历史查询，但不自动清除已有数据。Collection workflow evidence 独立持久化，在任务完成前不受群历史 retention 清理影响。历史只保存可读文本，不下载图片、不做 OCR、不转写语音、不解析文件或视频。OneBot 回补仅按用户查询和 Collection 检查触发，并受配置预算限制。

原 0.9.0 的投票表会在打开数据库时迁移：旧浏览器 ballot 只保留为 `legacy-web:*` 记录，无法还原 QQ 身份；新投票只使用群成员 QQ ID。任务调度和投票结束发布按持久化、at-least-once 语义工作。

## 🛡️ 安全模型

- Poll 群消息是 untrusted input；确定性解析优先，语义 fallback 有候选门槛、固定 Provider、8 秒超时、严格 JSON/证据校验且不使用 tools。
- 历史检索受 operator/当前群管理员权限域约束；跨群只允许私聊 operator，普通群成员不可查询其他群或当前群归档。
- Collection 解析失败不会删除原始消息；覆盖不完整会明确报告，未解决证据不会被静默 finalize。
- Relay 固定 `prepare → preview → explicit confirm → send`，失败不自动重试。
- Moderation 默认关闭，只支持单群单成员 mute、unmute、kick；独立 moderator 权限，`prepare → preview → confirm`，10 分钟 TTL，无自动重试。
- 群成员只能提交 Collection 或投票，不能查询归档、总结、Relay、清空数据或执行群管理。

## 🔧 Troubleshooting

### 插件加载失败

在 `WebUI → 插件` 查看错误，确认 AstrBot 满足 `>=4.25.5,<5`、依赖已安装，再尝试插件重载。不要删除 plugin data 作为排错手段。

### 投票没有记录

确认 Poll 仍为 OPEN、回复的是当前群的 Poll、选项使用完整编号/标签/reply key。多个 Poll 同时进行时使用 `投票 <P-ID> <选项>`。自然语言回复需要创建时保留可用 Provider；确定性回复不依赖 LLM。

### LLM checkpoint 不工作

检查 Collection 是否启用 `ai_extraction`、创建控制会话是否有 Provider、Provider 是否仍可用，以及 Collection 是否仍为 ACTIVE。

### 群历史显示 PARTIAL / UNKNOWN

检查 aiocqhttp/OneBot 历史 action 是否可用以及同步预算。`PARTIAL` 表示本地结果可能不完整，不应据此断言群里没有其他消息；可以缩小时间范围后重试，或由 operator 核对 OneBot/NapCat 实际可提供的历史范围。

### Excel 没收到

导出失败不会回滚统计结果；到 AstrBot plugin data directory 的 `exports/` 查找文件。QQ 文件回传可能因 OneBot/NapCat 能力失败。

## 🚧 当前限制与后续规划

当前尚未实现：

- 同一群同时运行多个 ACTIVE Collection；
- 图片归档、OCR、语音转写、文件内容提取；
- embedding、向量数据库、RAG、知识图谱；
- 批量 moderation、全员禁言、设置管理员、退群；
- QQ OAuth、实名投票、投票后台、问卷和复杂投票算法；
- 根据群总结自动创建任务。

真实 NapCat、QQ 群和 AstrBot Provider 环境仍建议做一次小范围验证，尤其是消息发送、文件回传和群管理权限。

## ✅ 开发验证

仓库验证包括：

```bash
python3 -m compileall .
python3 -m unittest discover -s tests -v
git diff --check
```

测试覆盖 Poll schema migration、原生消息解析、单选/多选、改票、截止、结果发布和旧功能回归；没有进行真实 LLM 消耗、真实 QQ 投票 E2E，也没有在真实群执行 mute/kick 或发送测试 spam。

## ⚖️ License

MIT License — see [LICENSE](LICENSE).
