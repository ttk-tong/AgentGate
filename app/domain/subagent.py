"""子 Agent 领域模型（plan/03 §8）。

子 agent 是「在隔离子上下文里跑一个受限的完整子 Loop，只回传最终文本」的机制。
`allowed_tools` 是**替换而非合并**——独立收紧权限，防止子 agent 拿到父 agent 的
全部工具。运行结果为纯文本，中间过程不进入父上下文（父 DAG 只留 sidechain 标记
事件供审计）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SubAgentSpec(BaseModel):
    """一次子 agent 派发的配置（plan/03 §8）。"""

    task: str                                      # 交给子 agent 的任务描述
    allowed_tools: list[str] = Field(default_factory=list)  # 替换（非合并）父工具集
    model: str | None = None                       # 可用更便宜的模型；None 复用父模型
    max_turns: int = 6                             # 子 agent 的最大轮数，独立于父
    max_tokens: int = 2048                         # 单次 LLM 请求 max_tokens
    system_prompt: str | None = None               # 子 agent 独立 system；None 用默认
    # 子 agent 通常只读 → 可 fan-out 并行。若需写侧作用，spawn_agent 的 spec
    # 也可标 is_read_only=False；此字段用于日志/审计，实际串并行由工具属性决定。
    read_only: bool = True
