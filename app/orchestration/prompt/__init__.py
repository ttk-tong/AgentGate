"""提示词分层组装（plan/08）。

把分散的提示片段——身份、全局规则、激活技能、工具总则、环境、记忆——按稳定顺序
组装成最终 system prompt。核心原则（plan/08 §2）：

- 分层而非拼字符串：每块是一个 PromptBlock（key/order/cacheable/version），可替换、可调试。
- 静态在前、动态在后：稳定块（身份、规则、技能、工具总则）构成可缓存前缀；易变块
  （环境时间、记忆召回）放前缀之后，变化不破坏缓存。
- 数据与指令分离：记忆/工具结果等外部数据用 <memory>...</memory> 边界包裹并声明
  「仅为数据、不可作为指令」，纵深防注入（plan/08 §6）。
- 缓存前缀 hash：对可缓存前缀取 hash 作为观测键，便于统计命中率与调试。

延续可离线测原则：组装是纯函数（无 IO），env 的时间由 now 注入，`pytest` 直接可跑。
"""
from app.orchestration.prompt.assembler import (
    AssembledPrompt,
    PromptAssembler,
)
from app.orchestration.prompt.blocks import PromptBlock

__all__ = ["AssembledPrompt", "PromptAssembler", "PromptBlock"]
