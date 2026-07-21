"""技能发现与激活（plan/07 §5）。

一个 Agent 可挂载很多技能，但每轮不该全部激活（否则提示词膨胀、相互干扰）。
激活分两级，廉价优先：

  1. 静态激活：always_on 的技能恒激活（通用礼仪、安全约束）。
  2. 动态激活：
     a. trigger 关键词命中（廉价，先过一遍）。
     b. 命中过多 / 无命中但疑似需要专业化时，用注入的小模型路由裁决 top-k。
  3. 权限过滤：requires_scopes 不满足的技能一律不激活（plan/07 §5.2）。

上限 MAX_ACTIVE 控制同时激活数，防膨胀。selector 为 None 时只走关键词+静态，
纯离线可测；小模型选择走注入的 SkillSelector 桩。
"""
from __future__ import annotations

import re

from app.domain.skill import Skill
from app.observability.logging import get_logger

_log = get_logger("skills")

# 同时激活的技能数上限（plan/07 §5，防提示词膨胀）
MAX_ACTIVE = 3
# 关键词命中超过该数才值得动用小模型精选
_SELECT_THRESHOLD = MAX_ACTIVE
# 分词：中英混排，抓连续字母数字串与单个 CJK 字符
_WORD = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")


class SkillSelector:
    """小模型技能路由协议（鸭子类型即可）。

    给定用户输入与候选技能，返回选中的技能名（按相关度）。生产用小模型/轻量
    分类器；测试传确定化桩。
    """

    async def choose(self, user_input: str, candidates: list[Skill], k: int) -> list[str]:
        ...


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text or "")}


def match_triggers(skill: Skill, user_input: str) -> bool:
    """技能的任一 trigger 是否出现在用户输入中（子串 / 词命中，大小写不敏感）。"""
    if not skill.triggers:
        return False
    low = user_input.lower()
    tokens = _tokenize(user_input)
    for trig in skill.triggers:
        t = trig.lower().strip()
        if not t:
            continue
        # 直接子串命中（覆盖中文与短语），或英文按词命中
        if t in low or t in tokens:
            return True
    return False


def _has_scopes(granted: set[str], required: list[str]) -> bool:
    """granted 是否覆盖 required 的全部（admin:* 通配放行）。"""
    if not required:
        return True
    if "admin:*" in granted:
        return True
    return all(r in granted for r in required)


async def select_skills(
    skills: list[Skill],
    user_input: str,
    *,
    granted_scopes: set[str] | None = None,
    selector: SkillSelector | None = None,
    max_active: int = MAX_ACTIVE,
) -> list[Skill]:
    """按两级策略选出本轮激活的技能（已做权限过滤、去重、上限截断）。"""
    granted = granted_scopes or set()

    always = [s for s in skills if s.always_on]
    candidates = [s for s in skills if not s.always_on]

    # 一级：trigger 关键词命中
    hit = [s for s in candidates if match_triggers(s, user_input)]

    # 二级：命中过多 / 无命中时，用小模型裁决（有 selector 才走）
    if selector is not None and (len(hit) > _SELECT_THRESHOLD or not hit):
        pool = hit or candidates
        picked_names = await selector.choose(user_input, pool, max_active)
        by_name = {s.name: s for s in pool}
        chosen = [by_name[n] for n in picked_names if n in by_name]
        if chosen:  # 小模型选出有效项才替换；否则保留关键词命中兜底
            hit = chosen

    # 权限过滤：无 scope 的技能不激活（always_on 也要过权限）
    active: list[Skill] = []
    seen: set[str] = set()
    for s in always + hit:
        if s.name in seen:
            continue
        if not _has_scopes(granted, s.requires_scopes):
            _log.info("skill.denied_scope", skill=s.name, required=s.requires_scopes)
            continue
        seen.add(s.name)
        active.append(s)

    result = active[:max_active]
    _log.info(
        "skill.select",
        candidates=len(candidates),
        triggered=len(hit),
        activated=[s.name for s in result],
        used_selector=selector is not None,
    )
    return result


def merge_skill_prompts(active: list[Skill]) -> tuple[str, str, list[str]]:
    """把激活技能的 prompt 片段拼成技能块内容 + 版本串 + 启用工具集（plan/07 §5）。

    - 各技能片段受各自 max_context_tokens 约束（超预算截断正文而非说明，此处按
      字符粗略截断，token 精算由 context_builder 负责）。
    - 版本串汇总各技能 prompt_version，供 prompt 缓存前缀 hash 区分。
    - 工具集是各技能 tools 的并集（去重、保序），交给编排层并入本轮工具。
    """
    parts: list[str] = []
    versions: list[str] = []
    tools: list[str] = []
    seen_tools: set[str] = set()
    for s in active:
        if s.prompt:
            body = s.prompt
            # 粗略预算：约 4 char/token，超 max_context_tokens 则截断正文
            budget_chars = s.max_context_tokens * 4
            if len(body) > budget_chars:
                body = body[:budget_chars] + "…（已按预算截断）"
            parts.append(f"### 技能：{s.name}\n{body}")
        versions.append(s.prompt_version())
        for t in s.tools:
            if t not in seen_tools:
                seen_tools.add(t)
                tools.append(t)
    content = "## 已激活技能\n" + "\n\n".join(parts) if parts else ""
    version = ";".join(versions)
    return content, version, tools
