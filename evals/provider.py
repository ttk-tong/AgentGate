"""评测专用 provider：给 MockProvider 加一层「嵌套脚本」能力。

为什么需要它：MockProvider 的指令正则 `\\[\\[tool:(.+?)\\]\\]` 是非贪婪的，
所以嵌套写法会被外层截断——

    [[tool:spawn_agent task=[[tool:weather city=北京]]]]
    → spawn_agent(task="[[tool:weather city=北京")   # 截断，子 agent 什么也不会调

委派题（D5）恰恰需要「让子 agent 也调工具」。做法是转义：内层写成
`<<tool:...>>`，并且**只在文本里没有外层 `[[tool:` 时**才还原成 `[[tool:...]]`。

    父层文本：拆一下 [[tool:spawn_agent task=子任务 <<tool:weather city=北京>>]]
             ↑ 含 [[tool: → 不还原，外层照常解析，task 原样带着 <<...>>
    子层文本：子任务 <<tool:weather city=北京>>
             ↑ 不含 [[tool: → 还原 → 子 agent 真的调 weather

被测代码路径一行没改：父子用的都是同一个 provider 接口、同一个 Loop、同一套
executor。换掉的只是题本的书写形式。
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator

from app.domain.llm import LLMRequest, StreamChunk
from app.domain.enums import Role
from app.domain.errors import ProviderOverloaded
from app.routing.providers.mock import MockProvider

_ESCAPED = re.compile(r"<<tool:(.+?)>>", re.DOTALL)

# 摘要请求的识别标记：compactor._summarize 固定用这句做 system。
# 认这个而不是认「messages 里有九段式骨架」：system 是摘要调用的稳定签名，
# 而摘要提示词的正文是会改的。
_SUMMARY_SYSTEM_MARK = "压缩对话上下文"


class SummarizerFailure(RuntimeError):
    """故意让摘要模型失败。compactor._summarize 会把它裹成 CompactionError，
    Loop 据此累计 consecutive_compact_failures → 熔断。走的是产品的真实失败路径。
    """


def unescape_nested(text: str) -> str:
    """把 `<<tool:...>>` 还原成 `[[tool:...]]`。

    只在**没有**外层指令时还原：有外层说明当前这层的解析目标是外层，
    提前还原会让外层的非贪婪正则截断在内层的第一个 `]]` 上。
    """
    if "[[tool:" in text:
        return text
    return _ESCAPED.sub(r"[[tool:\1]]", text)


# 被测那一轮 provider 实际收到的 system prompt——评测在这个边界上观测「召回是否
# 真的进了喂给模型的上下文」。召回只注入 system（PromptComposer → <memory> 块），
# Mock 不回显它、也没有记忆读取 API，所以这是唯一能证明「记忆进了 prompt」的观测点。
# 模块级而非实例级：provider 每题重装（_install_mock_provider），父子 agent 又共用
# 同一实例，模块级缓冲配合「测前 reset、测后读」最简单也最稳。
_CAPTURED_SYSTEMS: list[str] = []


def reset_captured_systems() -> None:
    """清空捕获缓冲。在被测那一轮发送前调用，隔掉 seed / 铺垫轮的 system。"""
    _CAPTURED_SYSTEMS.clear()


def captured_systems() -> list[str]:
    """返回自上次 reset 以来 provider 收到的所有 system prompt（每跳一条）。"""
    return list(_CAPTURED_SYSTEMS)


class EvalMockProvider(MockProvider):
    """MockProvider + 嵌套脚本还原 + 压缩/降级题需要的开关。

    tool_turns：允许连续几轮都产出工具调用。MockProvider 的规则是「本轮上下文里
    已有工具结果就收尾」，于是一次 run 最多 2 轮。压缩熔断题要的是「同一个 run 里
    连续 3 次压缩失败」，2 轮够不到。默认 1 → 与 MockProvider 行为完全一致。

    fail_summary：摘要请求直接抛错。

    overload_models：这些 model 名一律抛 ProviderOverloaded（429/503 的归一化），
    且抛在产出任何 chunk 之前——loop 只有「本轮尚未 emit」时才降级，emit 之后再失败
    只能报错。D7 降级题用：主模型过载 → loop 走降级链切到下一个 model；只有主模型
    在集合里则切过去成功（T01），整条链都在集合里则耗尽 → provider_unavailable（H01）。
    判定按 request.model（= LoopState.current_model），所以降级换名后同一 provider
    实例对新 model 名不再过载——这正是「切到备用模型后恢复」得以成立的观测点。

    刻意不用实例计数器记轮次，而是从请求里数「已带回结果的轮数」：provider 实例
    在父子 agent 之间是共用的（SubagentRunner 收的就是父的 provider），计数器会
    互相污染。从请求推导则是无状态的，父子各自算自己的。
    """

    name = "mock"  # 保持与 MockProvider 一致：路由/日志里不该出现一个陌生的 provider 名

    def __init__(
        self,
        delay_s: float = 0.0,
        *,
        tool_turns: int = 1,
        fail_summary: bool = False,
        overload_models: set[str] | None = None,
    ):
        super().__init__(delay_s=delay_s)
        self._tool_turns = max(1, tool_turns)
        self._fail_summary = fail_summary
        self._overload_models = overload_models or set()

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        _CAPTURED_SYSTEMS.append(request.system or "")
        # D7 降级题：被指定的 model 一律过载。抛在产出任何 chunk 之前——loop 只有
        # 「本轮尚未 emit」时才走降级分支（agent_loop 的 emitted_any），emit 之后
        # 再失败只能报错。按 request.model 判定：降级换了 model 名后就不再命中。
        if request.model in self._overload_models:
            raise ProviderOverloaded(
                f"eval: model {request.model!r} forced overloaded"
            )
        if self._fail_summary and _SUMMARY_SYSTEM_MARK in (request.system or ""):
            raise SummarizerFailure("eval: summarizer forced to fail")

        patched = request.model_copy(
            update={
                "messages": [
                    m.model_copy(update={"content": unescape_nested(m.content)})
                    if m.content
                    else m
                    for m in request.messages
                ]
            }
        )

        if self._tool_turns > 1:
            done_turns = sum(1 for m in patched.messages if m.tool_results)
            if done_turns < self._tool_turns:
                # 把已回填的结果从这一份请求里摘掉，MockProvider 便认不出
                # 「已经调过工具」，于是照着 user 文本里的指令再调一轮。
                # 只改喂给 Mock 的副本，落库的上下文一个字节没动。
                patched = patched.model_copy(
                    update={
                        "messages": [
                            m for m in patched.messages if not m.tool_results
                        ]
                    }
                )
                # 指令在最近一条 user 文本里；摘掉工具结果后它仍是最近的 user。
                if not any(m.role == Role.user for m in patched.messages):
                    patched = request  # 兜底：不该发生，宁可退回原请求也不要空 messages

        async for chunk in super().stream(patched):
            yield chunk
