"""提示词分层组装离线单测（plan/08）。

纯逻辑：模板/技能片段/召回记忆都是入参，now 注入，`pytest` 不碰 DB/时钟。
覆盖：块顺序、缓存前缀 hash 稳定性、动态块不破坏前缀、记忆防注入包裹、
技能块归入前缀。
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.domain.memory import MemoryItem, MemoryKind, MemoryScope
from app.orchestration.prompt.assembler import PromptAssembler

_NOW = datetime(2026, 7, 21, tzinfo=timezone.utc).isoformat(timespec="seconds")


def _assembler() -> PromptAssembler:
    return PromptAssembler(agent_name="AgentGate", agent_role="一个有帮助的助手")


def test_block_order_static_before_dynamic():
    a = _assembler().assemble(now_iso=_NOW)
    keys = [b.key for b in a.blocks]
    # identity/rules/tools_hint（静态）在 env（动态）之前
    assert keys.index("identity") < keys.index("rules") < keys.index("tools_hint")
    assert keys.index("tools_hint") < keys.index("env")


def test_cache_prefix_hash_stable_across_dynamic_change():
    """动态块（env 时间）变化不改缓存前缀 hash（plan/08 §7）。"""
    a1 = _assembler().assemble(now_iso=_NOW)
    a2 = _assembler().assemble(now_iso="2099-01-01T00:00:00+00:00")
    assert a1.cache_prefix_hash == a2.cache_prefix_hash


def test_cache_prefix_hash_changes_with_skill():
    """激活技能进入缓存前缀 → hash 改变。"""
    a1 = _assembler().assemble(now_iso=_NOW)
    a2 = _assembler().assemble(
        now_iso=_NOW, skill_prompt="发票处理领域提示", skill_prompt_version="invoice/1.0"
    )
    assert a1.cache_prefix_hash != a2.cache_prefix_hash
    assert "发票处理领域提示" in a2.system


def test_memory_block_wraps_with_boundary():
    """召回记忆用 <memory> 边界包裹并声明为数据（plan/08 §6 防注入）。"""
    item = MemoryItem(
        id=str(uuid4()),
        scope=MemoryScope.user,
        scope_key="u1",
        kind=MemoryKind.preference,
        content="用户偏好简体中文、简洁回答",
    )
    a = _assembler().assemble(now_iso=_NOW, recalled_memory=[item])
    assert "<memory" in a.system and "</memory>" in a.system
    assert "用户偏好简体中文" in a.system
    # 记忆块是动态的，不进缓存前缀
    mem_block = next(b for b in a.blocks if b.key == "memory")
    assert mem_block.cacheable is False


def test_no_memory_no_memory_block():
    a = _assembler().assemble(now_iso=_NOW)
    assert all(b.key != "memory" for b in a.blocks)
