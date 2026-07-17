"""路由层：把请求解析成 RouteDecision（首选 target + 降级链），并按需求校验能力（plan/01 §2）。

阶段 4 落地一个**静态策略路由**：从 agent.model_policy（或配置默认）取首选模型与降级链，
过滤熔断打开的 Provider，按能力（工具/视觉/上下文窗口）粗筛。真正的健康度打分、
延迟感知路由留待后续；此处先给出稳定接口 + 降级链，供 call_with_retry 消费。

纯数据变换（不含 IO），可离线单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.context.context_builder import model_context_window


@dataclass
class Capability:
    """本次调用的能力需求（plan/01 §2.1）。"""

    tools: bool = False
    vision: bool = False
    json_mode: bool = False
    min_context: int = 0


@dataclass
class RouteDecision:
    """路由结果：首选 target + 降级链（plan/01 §2.1）。"""

    provider: str
    model: str
    params: dict = field(default_factory=dict)
    fallbacks: list[tuple[str, str]] = field(default_factory=list)

    def targets(self) -> list[tuple[str, str]]:
        """[(provider, model), ...]：首选在前，降级链在后。供 call_with_retry 用。"""
        return [(self.provider, self.model), *self.fallbacks]


# 已知模型的能力画像（粗粒度）。未知模型按保守默认（支持工具、不支持视觉）。
_MODEL_CAPS: dict[str, dict] = {
    "deepseek": {"tools": True, "vision": False},
    "claude-opus": {"tools": True, "vision": True},
    "claude-sonnet": {"tools": True, "vision": True},
    "claude-haiku": {"tools": True, "vision": True},
    "gpt-4o": {"tools": True, "vision": True},
    "mock": {"tools": True, "vision": True},
}


def _caps_for(model: str) -> dict:
    name = (model or "").lower()
    for prefix, caps in _MODEL_CAPS.items():
        if name.startswith(prefix):
            return caps
    return {"tools": True, "vision": False}


def model_supports(model: str, need: Capability) -> bool:
    """模型是否满足能力需求（工具/视觉/上下文窗口）。"""
    caps = _caps_for(model)
    if need.tools and not caps.get("tools", False):
        return False
    if need.vision and not caps.get("vision", False):
        return False
    if need.min_context and model_context_window(model) < need.min_context:
        return False
    return True


class ModelRouter:
    """静态策略路由：按 policy 选首选 + 降级链，过滤熔断与能力不匹配的 target。"""

    def __init__(self, default_provider: str, default_model: str):
        self._default_provider = default_provider
        self._default_model = default_model

    def resolve(
        self,
        need: Capability,
        *,
        policy: dict | None = None,
        override_model: str | None = None,
        open_providers: set[str] | None = None,
    ) -> RouteDecision:
        """解析路由决策（plan/01 §2.2 优先级：显式覆盖 → policy → 默认）。

        - override_model：显式指定则直连（仍校验能力，不满足抛 ValueError）。
        - policy：{"provider","model","fallbacks":[[provider,model],...],"params":{}}。
        - open_providers：熔断打开的 Provider 集合，从候选中剔除。
        全部候选被能力/熔断过滤光 → 抛 ValueError（交由上层映射 503）。
        """
        open_providers = open_providers or set()
        params = dict((policy or {}).get("params", {}))

        # 1. 显式覆盖：直连指定模型（Provider 沿用默认或 policy 首选）
        if override_model:
            provider = (policy or {}).get("provider", self._default_provider)
            if not model_supports(override_model, need):
                raise ValueError(f"override model {override_model} lacks capability {need}")
            return RouteDecision(provider=provider, model=override_model, params=params)

        # 2. 组装候选链：policy 首选 + 降级链，否则默认单点
        candidates: list[tuple[str, str]] = []
        if policy and policy.get("model"):
            candidates.append((policy.get("provider", self._default_provider), policy["model"]))
            for fb in policy.get("fallbacks", []):
                candidates.append((fb[0], fb[1]))
        else:
            candidates.append((self._default_provider, self._default_model))

        # 3. 过滤：熔断打开 + 能力不匹配
        viable = [
            (p, m)
            for (p, m) in candidates
            if p not in open_providers and model_supports(m, need)
        ]
        if not viable:
            raise ValueError("no viable route (all filtered by circuit/capability)")

        head, *rest = viable
        return RouteDecision(provider=head[0], model=head[1], params=params, fallbacks=rest)
