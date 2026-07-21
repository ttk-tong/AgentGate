"""技能领域模型（plan/07 §3）。

技能 = 提示词片段 + 一组工具 + 使用说明 + 元数据的可复用打包。声明式清单
SKILL.md（front-matter + 正文）描述元数据与领域提示；加载时解析成 Skill。

triggers 用于廉价的关键词自动发现；tools 引用工具注册表中的名字（加载时校验存在）；
requires_scopes 是激活所需权限；max_context_tokens 约束该技能提示片段的预算上限。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Skill(BaseModel):
    """一个技能的声明 + 正文提示。prompt 是注入 system 的领域片段。"""

    name: str
    version: str = "0.0.0"
    description: str = ""
    triggers: list[str] = Field(default_factory=list)      # 自动发现关键词
    tools: list[str] = Field(default_factory=list)         # 启用的工具名（引用注册表）
    requires_scopes: list[str] = Field(default_factory=list)
    model_hint: str | None = None                          # 供路由参考（plan/01）
    max_context_tokens: int = 2000                         # 提示片段预算上限
    always_on: bool = False                                # 静态激活（如安全约束）
    prompt: str = ""                                       # SKILL.md 正文（领域提示片段）

    def prompt_version(self) -> str:
        """技能提示片段的版本标识，供 prompt 缓存前缀 hash 区分（plan/08 §7）。"""
        return f"{self.name}/{self.version}"
