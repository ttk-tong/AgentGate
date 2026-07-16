"""Provider 选择。

阶段 1 无真实路由，按配置择一：
- 配了 OpenAI 兼容端点（openai_api_key + openai_base_url）→ OpenAICompatProvider
- 配了 Anthropic key → AnthropicProvider
- 都没配 → Mock，让 walking skeleton 在无网络/无密钥时也能端到端跑通。
真正的路由/降级链见 plan/01，后续阶段替换此处。
"""
from __future__ import annotations

from app.config import get_settings
from app.routing.providers.anthropic import AnthropicProvider
from app.routing.providers.base import Provider
from app.routing.providers.mock import MockProvider
from app.routing.providers.openai_compat import OpenAICompatProvider


def get_provider() -> Provider:
    settings = get_settings()
    if settings.openai_api_key and settings.openai_base_url:
        return OpenAICompatProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    if settings.anthropic_api_key:
        return AnthropicProvider(api_key=settings.anthropic_api_key)
    return MockProvider()
