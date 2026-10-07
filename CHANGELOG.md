# Changelog

## 0.12.0 — Unreleased

### Collection

- 新增 `required_when` 条件必填；旧任务仍保持所有字段必填，完成状态统一用于 status、chase、checkpoint recheck、finalize 和 XLSX missing sheet。
- 多字段 Collection 支持唯一有限值裸回复；重复别名时不猜测，字段格式提交仍可明确解析。
- 默认 Collection 公告展示有限值映射和条件字段；`collection_ack` 默认关闭并尊重显式配置。
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
