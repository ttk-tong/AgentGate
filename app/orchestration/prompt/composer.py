"""提示词编排门面：记忆召回 + 技能激活 + 分层组装（plan/06 §5、07 §5、08 §4）。

把阶段 6 的三块能力收敛成一次调用，供 Agent Loop 在每轮用户输入前调用：

    composed = await composer.compose(user_text, ctx)
    # composed.system        → 组装好的 system prompt（含召回记忆、激活技能片段）
    # composed.enabled_tools  → 基础工具 ∪ 激活技能启用的工具
    # composed.debug          → dry-run 观测（缓存前缀 hash、各块、激活技能）

三步：
  1. 召回记忆：按会话 scope（user/agent/session）+ 当前输入召回 top-k（memory/recall）。
  2. 激活技能：trigger 关键词命中 + （可选）小模型精选 + scope 过滤（skills/selector）。
  3. 分层组装：静态前缀 + 技能块 + 动态 env/memory 块，算缓存前缀 hash（prompt/assembler）。

全部依赖可注入（store/selector/registry/assembler + now），无一者时优雅降级：
无记忆服务→跳过召回；无技能注册表→只用基础工具；纯逻辑可离线测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.context.memory.recall import DEFAULT_TOP_K, MemoryService
from app.orchestration.prompt.assembler import PromptAssembler
from app.orchestration.skills.registry import SkillRegistry
from app.orchestration.skills.selector import (
    SkillSelector,
    merge_skill_prompts,
    select_skills,
)


@dataclass
class ComposeContext:
    """一次组装所需的会话/环境上下文（由 Loop/API 从会话元数据填充）。"""

    now_iso: str
    tenant_id: str | None = None
    external_user: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    language: str = "简体中文"
    granted_scopes: set[str] = field(default_factory=set)


@dataclass
class ComposedPrompt:
    system: str
    enabled_tools: list[str] | None      # None = 不改动 Loop 的默认工具集
    activated_skills: list[str]
    recalled_count: int
    debug: dict


class PromptComposer:
    """记忆 + 技能 + 组装的门面。各依赖可选，缺谁降级谁。"""

    def __init__(
        self,
        assembler: PromptAssembler,
        *,
        memory: MemoryService | None = None,
        skills: SkillRegistry | None = None,
        skill_selector: SkillSelector | None = None,
        base_tools: list[str] | None = None,
        recall_k: int = DEFAULT_TOP_K,
    ):
        self._assembler = assembler
        self._memory = memory
        self._skills = skills
        self._skill_selector = skill_selector
        self._base_tools = base_tools
        self._recall_k = recall_k

    async def compose(self, user_text: str, ctx: ComposeContext) -> ComposedPrompt:
        # 1) 召回记忆（有记忆服务才走）
        recalled = []
        if self._memory is not None:
            recalled = await self._memory.recall(
                user_text,
                tenant_id=ctx.tenant_id,
                external_user=ctx.external_user,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
                k=self._recall_k,
            )

        # 2) 激活技能（有注册表才走）
        skill_prompt = None
        skill_version = None
        enabled_tools: list[str] | None = None
        activated: list[str] = []
        if self._skills is not None:
            active = await select_skills(
                self._skills.all(),
                user_text,
                granted_scopes=ctx.granted_scopes,
                selector=self._skill_selector,
            )
            activated = [s.name for s in active]
            content, version, skill_tools = merge_skill_prompts(active)
            if content:
                skill_prompt = content
                skill_version = version
            # 工具集 = 基础工具 ∪ 技能工具（保序去重）；无基础集则仅在有技能工具时给出
            if self._base_tools is not None or skill_tools:
                merged = list(self._base_tools or [])
                seen = set(merged)
                for t in skill_tools:
                    if t not in seen:
                        seen.add(t)
                        merged.append(t)
                enabled_tools = merged

        # 3) 分层组装
        assembled = self._assembler.assemble(
            language=ctx.language,
            tenant=ctx.tenant_id,
            now_iso=ctx.now_iso,
            skill_prompt=skill_prompt,
            skill_prompt_version=skill_version,
            recalled_memory=recalled or None,
        )

        debug = {
            **assembled.debug(),
            "activated_skills": activated,
            "recalled_memory": len(recalled),
            "enabled_tools": enabled_tools,
        }
        return ComposedPrompt(
            system=assembled.system,
            enabled_tools=enabled_tools,
            activated_skills=activated,
            recalled_count=len(recalled),
            debug=debug,
        )
