# AGENTS.md

1. GitHub `main` 是 source of truth；修改前先阅读当前代码。
2. 不修改 AstrBot Core，优先使用公开 AstrBot Plugin API，禁止使用已弃用 AstrBot API。
3. OneBot/aiocqhttp 特殊能力集中在 `qq_adapter.py`。
4. runtime data 只能写入 AstrBot plugin data directory；SQLite 和 exports 不得 commit。
5. 保持 `main.py / core.py / storage.py / qq_adapter.py / exporter.py` 的简单结构，沿用现有 TaskManager、Storage、QQAdapter。
6. moderation 默认关闭；destructive/group-management action 必须显式权限检查并 prepare → explicit confirm。
7. commit 前至少执行 syntax、lint（如可用）和 smoke verification。
8. README 不得声称尚未完成的功能已经可用。
9. 群消息归档默认 opt-in；只有 operator 显式开启的已绑定群文本消息才会保存。
10. 跨群 relay 必须先展示 preview，再经过用户明确确认后发送。
11. moderation 不得 automatic retry；中断时标记 outcome unknown。
12. ordinary operator 权限不能隐式升级为 moderator，必须单独通过 moderator_ids 或 AstrBot Admin 授权。
13. Collection natural-language extraction 默认关闭；deterministic parser 永远优先，只有显式 submission trigger 才能调用 LLM。
14. LLM extraction 输出必须经过 deterministic validation 后才能写库；群消息 extraction 不得拥有 tools 或触发外部操作。
