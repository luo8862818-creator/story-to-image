---
name: story-to-image
description: 通过Python持久管理故事、脚本或参考图任务，调用ChatGPT/Codex原生生图；保留脚本命名，支持限量分批、跨聊天续作、局部修改和重生成，不接其他生图API。
---

先读[主技能](../../../SKILL.md)，以主技能目录解析Python入口scripts/project.py及文档。需要Python 3.11+和Pillow；原生工具生图，不接图片API。续作先status读取项目state.sqlite3。
