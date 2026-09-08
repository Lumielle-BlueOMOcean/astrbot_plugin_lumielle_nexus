# AGENTS.md

1. GitHub `main` 是 source of truth；修改前先阅读当前代码。
2. 不修改 AstrBot Core，优先使用公开 AstrBot Plugin API，禁止使用已弃用 AstrBot API。
3. OneBot/aiocqhttp 特殊能力集中在 `qq_adapter.py`。
4. runtime data 只能写入 AstrBot plugin data directory；SQLite 和 exports 不得 commit。
5. 保持 `main.py / core.py / storage.py / qq_adapter.py / exporter.py` 的简单结构，沿用现有 TaskManager、Storage、QQAdapter。
6. destructive moderation 必须有显式权限检查和确认机制；本版不实现此类能力。
7. commit 前至少执行 syntax、lint（如可用）和 smoke verification。
8. README 不得声称尚未完成的功能已经可用。
