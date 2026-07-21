"""技能加载 + 激活离线单测（plan/07）。

front-matter 解析、trigger 命中、scope 过滤、小模型精选都是纯逻辑，
`pytest` 不碰磁盘/LLM（loader 支持从内存文本解析，selector 用桩）。
"""
from __future__ import annotations

import pytest

from app.domain.skill import Skill
from app.orchestration.skills.loader import parse_skill_md
from app.orchestration.skills.registry import SkillRegistry
from app.orchestration.skills.selector import (
    match_triggers,
    merge_skill_prompts,
    select_skills,
)

_SKILL_MD = """---
name: invoice_processing
version: 1.2.0
description: 处理发票/报销单
triggers: [发票, 报销, invoice]
tools: [kb_search, sql_query]
requires_scopes: [finance:read]
max_context_tokens: 1500
---
你是发票处理专家，从发票中抽取字段并校验。
"""


# —— loader ——


def test_parse_front_matter_and_body():
    s = parse_skill_md(_SKILL_MD)
    assert s.name == "invoice_processing"
    assert s.version == "1.2.0"
    assert s.triggers == ["发票", "报销", "invoice"]
    assert s.tools == ["kb_search", "sql_query"]
    assert s.requires_scopes == ["finance:read"]
    assert s.max_context_tokens == 1500
    assert "发票处理专家" in s.prompt


def test_parse_missing_name_raises():
    with pytest.raises(ValueError):
        parse_skill_md("---\nversion: 1.0\n---\n正文")


def test_parse_pure_prompt_with_minimal_front_matter():
    # 无 front-matter（缺 name）应抛；带最简 name 的纯正文技能正常解析
    with pytest.raises(ValueError):
        parse_skill_md("就是一段提示词")
    s = parse_skill_md("---\nname: x\n---\n就是一段提示词")
    assert s.name == "x" and s.prompt == "就是一段提示词"


# —— registry ——


def test_registry_rejects_unknown_tool():
    reg = SkillRegistry()
    skill = parse_skill_md(_SKILL_MD)
    with pytest.raises(ValueError):
        reg.register(skill, known_tools={"kb_search"})  # 缺 sql_query


def test_registry_accepts_known_tools():
    reg = SkillRegistry()
    skill = parse_skill_md(_SKILL_MD)
    reg.register(skill, known_tools={"kb_search", "sql_query"})
    assert reg.get("invoice_processing") is not None


# —— trigger 命中 ——


def test_match_triggers_cn_substring():
    s = parse_skill_md(_SKILL_MD)
    assert match_triggers(s, "帮我处理这张发票")
    assert match_triggers(s, "invoice help please")
    assert not match_triggers(s, "今天天气怎么样")


# —— select_skills ——


def _skill(name, triggers=None, scopes=None, always_on=False, tools=None):
    return Skill(
        name=name,
        triggers=triggers or [],
        requires_scopes=scopes or [],
        always_on=always_on,
        tools=tools or [],
        prompt=f"{name} 提示",
    )


async def test_select_always_on_plus_trigger():
    skills = [
        _skill("safety", always_on=True),
        _skill("invoice", triggers=["发票"]),
        _skill("weather", triggers=["天气"]),
    ]
    active = await select_skills(skills, "帮我看这张发票")
    names = [s.name for s in active]
    assert "safety" in names and "invoice" in names and "weather" not in names


async def test_select_scope_filter():
    skills = [_skill("finance", triggers=["报销"], scopes=["finance:read"])]
    # 无 scope → 不激活
    active = await select_skills(skills, "报销", granted_scopes=set())
    assert active == []
    # 有 scope → 激活
    active = await select_skills(skills, "报销", granted_scopes={"finance:read"})
    assert [s.name for s in active] == ["finance"]


async def test_select_uses_selector_when_no_trigger_hit():
    """无 trigger 命中但有小模型 → 用小模型裁决。"""
    skills = [_skill("a"), _skill("b"), _skill("c")]

    class Stub:
        async def choose(self, user_input, candidates, k):
            return ["b"]

    active = await select_skills(skills, "随便说点什么", selector=Stub())
    assert [s.name for s in active] == ["b"]


async def test_select_respects_max_active():
    skills = [_skill(f"s{i}", triggers=["go"]) for i in range(5)]
    active = await select_skills(skills, "go", max_active=2)
    assert len(active) == 2


# —— merge_skill_prompts ——


def test_merge_prompts_and_tools():
    skills = [
        _skill("a", tools=["kb_search"]),
        _skill("b", tools=["kb_search", "sql_query"]),
    ]
    content, version, tools = merge_skill_prompts(skills)
    assert "a 提示" in content and "b 提示" in content
    assert tools == ["kb_search", "sql_query"]  # 去重保序
    assert "a/0.0.0" in version and "b/0.0.0" in version


def test_merge_prompts_budget_truncation():
    big = _skill("big")
    big.prompt = "x" * 100
    big.max_context_tokens = 10  # 预算 ~40 char
    content, _, _ = merge_skill_prompts([big])
    assert "已按预算截断" in content
