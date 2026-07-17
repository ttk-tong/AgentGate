"""Provider 层向上抛出的可恢复错误（见 plan/03 §4 恢复路径）。

这些异常是 Provider 适配器与 Agent Loop 之间的契约：Loop 据此选择恢复策略
（反应式压缩、模型降级），每条恢复路径都带一次性/有上限的 guard，防死循环。
"""
from __future__ import annotations


class ProviderError(Exception):
    """Provider 调用类错误基类。"""


class PromptTooLong(ProviderError):
    """提示超长（HTTP 413 / prompt_too_long）。触发反应式压缩兜底（03 §4）。"""


class ProviderOverloaded(ProviderError):
    """Provider 过载（429/503）。触发模型降级（03 §5）。阶段 3 暂不接。"""
