"""内置工具集合（见 plan/04 §9）。

- kb_search（只读，可并发）
- file_read（只读，可并发）
- note_append（有副作用，串行；验证读写分批与延迟应用）

装配入口见上层 app.orchestration.tools.build_default_registry。
真实部署时工具集由 Agent 配置 + 激活技能过滤（见 plan/07），此处给全集。
"""
