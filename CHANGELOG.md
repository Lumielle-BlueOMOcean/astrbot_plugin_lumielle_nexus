# Changelog

## 0.12.0 — Unreleased

### Collection Reliability

- 群内消息先持久化为 Collection workflow evidence，并按来源消息 ID/序号去重；OneBot 有界历史回补保留原始发送时间，并在 checkpoint、chase、refresh、finalize 前协调本地证据。
- checkpoint 按时间顺序增量处理；Provider/校验不完整时保留原文、不推进未解决证据游标。`no_data` 在 chase/finalize 时最多复核一次；历史 coverage 不完整且可能影响缺席判断时抑制催办、缺省值和完成状态。
- 未开启 AI 提取的 Collection 仅处理可确定解析的提交；不匹配的普通聊天保留为 `ignored` 审计证据，不计为提交，也不阻塞 checkpoint/finalize。
- 条件必填使用统一 `COMPLETE / PARTIAL / NO_RESPONSE` 判定，并一致应用于统计、催办、最终缺省值和 XLSX 未提交名单。

### Group History

- 新增持久化群文本历史与有界 OneBot 回补，支持消息身份去重、保守的 `FULL / PARTIAL / LOCAL_ONLY / UNKNOWN` coverage，以及页数、消息数和时间预算。
- 群历史默认开启，可按群显式关闭；关闭后停止新增归档和历史查询，不删除已有数据。私聊 operator 可查询已绑定群；群内查询仅限当前群 owner/admin。
- 增强历史查询分页、coverage 和返回预算说明；群聊原文作为不可信数据处理。

### Collection

- 新增 `required_when` 条件必填；旧任务仍保持所有字段必填，完成状态统一用于 status、chase、checkpoint recheck、finalize 和 XLSX missing sheet。
- 多字段 Collection 支持唯一有限值裸回复；重复别名时不猜测，字段格式提交仍可明确解析。
- 默认 Collection 公告展示有限值映射和条件字段；AI 提取模式仅附加简短通用填写提示，自定义公告保持原文，不再追加返校/车票改签示例或内部 checkpoint 术语。
- `collection_ack` 默认关闭并尊重显式配置。
- 历史查询工具补充自适应返回数量、分页、coverage 和 token 使用边界说明，不改变同步预算或查询算法。

## 0.11.0 — 2026-09-19

### 新增

- 原生 QQ 群消息投票公告支持可选 `@全体成员`，默认仍不 @全体。
- Poll 选项上限提升至 50 个，保留单选、多选、自定义回复 key 和确定性解析行为。

### 兼容性

- 本次发布不修改 AstrBot Core、SQLite schema 或已有任务/Collection/Poll 数据格式。
- 继续支持 AstrBot `>=4.25.5,<5` 与 `aiocqhttp` / OneBot v11。

### 安全与限制

- `@全体成员` 只影响创建 Poll 时发送的群公告，不会改变投票回复、结果发布或权限模型。
- 语义投票仍受候选门槛、固定 Provider 和严格结果校验限制；普通群聊不会逐条触发 LLM。
