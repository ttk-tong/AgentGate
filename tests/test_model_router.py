"""路由层单测：能力校验 + 降级链 + 熔断过滤（plan/01 §2）。

纯数据变换，无 DB/网络，`pytest` 或 `python -c` 直接可跑。
"""
from __future__ import annotations

import pytest

from app.routing.model_router import Capability, ModelRouter, model_supports


def test_model_supports_capability():
    # deepseek 支持工具、不支持视觉
    assert model_supports("deepseek-v4-pro", Capability(tools=True))
    assert not model_supports("deepseek-v4-pro", Capability(vision=True))
    # claude-opus 两者都支持
    assert model_supports("claude-opus-4-8", Capability(tools=True, vision=True))


def test_model_supports_min_context():
    # deepseek 窗口 128k：要求 200k 上下文不满足，要求 64k 满足
    assert not model_supports("deepseek-v4-pro", Capability(min_context=200_000))
    assert model_supports("deepseek-v4-pro", Capability(min_context=64_000))


def test_resolve_default_when_no_policy():
    router = ModelRouter("openai_compat", "deepseek-v4-pro")
    decision = router.resolve(Capability())
    assert decision.provider == "openai_compat"
    assert decision.model == "deepseek-v4-pro"
    assert decision.fallbacks == []
    # targets 首选在前
    assert decision.targets() == [("openai_compat", "deepseek-v4-pro")]


def test_resolve_policy_with_fallbacks():
    router = ModelRouter("openai_compat", "deepseek-v4-pro")
    policy = {
        "provider": "openai_compat",
        "model": "deepseek-v4-pro",
        "fallbacks": [["openai_compat", "deepseek-v4-flash"], ["anthropic", "claude-haiku-4-5"]],
        "params": {"temperature": 0.7},
    }
    decision = router.resolve(Capability(), policy=policy)
    assert decision.model == "deepseek-v4-pro"
    assert decision.params == {"temperature": 0.7}
    assert decision.targets() == [
        ("openai_compat", "deepseek-v4-pro"),
        ("openai_compat", "deepseek-v4-flash"),
        ("anthropic", "claude-haiku-4-5"),
    ]


def test_resolve_filters_open_circuit_provider():
    """熔断打开的 Provider 从候选剔除，首选顺延到降级链。"""
    router = ModelRouter("openai_compat", "deepseek-v4-pro")
    policy = {
        "provider": "openai_compat",
        "model": "deepseek-v4-pro",
        "fallbacks": [["anthropic", "claude-opus-4-8"]],
    }
    decision = router.resolve(Capability(), policy=policy, open_providers={"openai_compat"})
    # openai_compat 被熔断过滤，anthropic 成为首选
    assert decision.provider == "anthropic"
    assert decision.model == "claude-opus-4-8"


def test_resolve_raises_when_all_filtered():
    router = ModelRouter("openai_compat", "deepseek-v4-pro")
    with pytest.raises(ValueError):
        router.resolve(Capability(), open_providers={"openai_compat"})


def test_override_model_direct():
    router = ModelRouter("openai_compat", "deepseek-v4-pro")
    decision = router.resolve(Capability(), override_model="deepseek-v4-pro-max")
    assert decision.model == "deepseek-v4-pro-max"


def test_override_model_capability_rejected():
    router = ModelRouter("openai_compat", "deepseek-v4-pro")
    # 强制要求视觉，但 deepseek 不支持 → 覆盖也被拒
    with pytest.raises(ValueError):
        router.resolve(Capability(vision=True), override_model="deepseek-v4-pro")
