"""测试夹具：强制 e2e 测试走 Mock Provider，保证确定性、离线可跑。

生产 .env 可能配了真实 provider key（DeepSeek 等），但端到端测试要的是
可复现、无网络依赖的行为。这里把 chat.py 用到的 get_provider 固定为 Mock，
既让工具脚本化（[[tool:...]]）生效，也避免真实 API 的限流/波动干扰断言。
"""
from __future__ import annotations

import pytest

from app.routing.providers.mock import MockProvider


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    monkeypatch.setattr("app.api.v1.chat.get_provider", lambda: MockProvider())
