"""提示块模型与标准顺序（plan/08 §3）。"""
from __future__ import annotations

from pydantic import BaseModel

# 标准块顺序（plan/08 §3）。小 order 在前；cacheable 块构成缓存前缀。
ORDER_IDENTITY = 0       # Agent 身份、角色、语气
ORDER_RULES = 10         # 安全约束、输出规范、拒绝策略
ORDER_SKILLS = 20        # 激活技能的 prompt 片段拼接（见 07）
ORDER_TOOLS_HINT = 30    # 工具使用总则（具体 schema 走 function-calling 通道）
ORDER_ENV = 40           # 当前时间、租户、语言、会话元信息（动态）
ORDER_MEMORY = 50        # 长期记忆召回（见 06），带来源标注（动态）
ORDER_TASK_HINT = 60     # 本轮任务引导（可选，动态）


class PromptBlock(BaseModel):
    """一个提示块。cacheable=True 的块按 order 排序后构成可缓存前缀。"""

    key: str            # identity | rules | skills | tools_hint | env | memory | ...
    content: str
    order: int
    cacheable: bool
    version: str | None = None
