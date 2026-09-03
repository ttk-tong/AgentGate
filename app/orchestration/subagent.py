"""子 Agent 隔离执行（plan/03 §8）。

`SubagentRunner` 是 `spawn_agent` 工具的执行体。给定一个 `SubAgentSpec`，跑一个
**受限的完整子 Loop**，返回子 agent 的最终文本回给父。

关键性质：
- **隔离**：子 agent 有自己的事件流（in-memory，不写父 DAG），独立工具集
  （`allowed_tools` 替换而非合并，且再与父的可用工具集**求交**），可用更便宜的模型。
- **只返最终文本**：中间推理与工具调用只在子 agent 内部循环，父只拿到
  `run()` 的返回值——中间过程不污染父上下文。
- **权限不放大**：父的 tenant_id / granted_scopes 原样透传给子的 ToolContext。
  子 agent 只能是父权限的**子集**，不能是超集——这是安全底线，别指望
  「反正子 agent 只读」。
- **审计留痕**：一条 `is_sidechain=True` 的标记事件记录派发的任务与最终结果，
  但**不进入父投影**（见 projection.build_main_chain 与 session_store.append_event
  对 sidechain 的特殊处理）。该事件由**父 loop** 在批结束后串行写入（见下）。
- **可 fan-out**：`spawn_agent` 工具本身是只读+并发安全的，多次调用会被
  tool_executor 归入并发批（plan/04 §8），从而在同一轮里并行派发多个子 agent。

为什么审计事件不在这里写：`spawn_agent` 会 fan-out，多个子 agent 在同一个并发批
里跑，而父的 `AsyncSession` 不是并发安全的——在子协程里直接 append_event 等于让
多个协程同时用一个 session。所以 runner 只产出 outcome，由 `spawn_agent` 挂成
`ContextMutation`，executor 在批结束后**按调用顺序串行**交给父 loop 落库
（见 tool_executor 的副作用延迟应用）。

刻意不做的事（保持精简）：
- 不接压缩/记忆召回/技能激活——子 agent 用途是「短平快子任务」，重量级流水线
  留给父。真需要 sub-agent 也能跑重逻辑再另外注入。
- 不接 dangerous 确认流程——子 agent 通常只读；写入类工具应留给父串行执行。
"""
from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel

from app.domain.enums import Role
from app.domain.errors import ProviderError
from app.domain.llm import LLMMessage, LLMRequest, ToolCall
from app.domain.subagent import SubAgentSpec
from app.domain.tool import ToolContext, ToolResult
from app.observability.logging import get_logger
from app.orchestration.tool_executor import execute_batched
from app.orchestration.tools.base import ToolRegistry
from app.routing.providers.base import Provider

log = get_logger("subagent")

_DEFAULT_SYSTEM = (
    "你是一个专注的子 agent，负责完成父 agent 派发的子任务。"
    "只使用被授权的工具，简洁作答；给出可直接被父采纳的最终结论。"
)

# 永不下传给子 agent 的工具。spawn_agent 必须在这里：子 agent 复用父的 registry，
# 里面那个 spawn_agent 持有同一个 runner，于是子能派孙、孙能派曾孙——没有深度计数
# 器的情况下这是指数级 fan-out，一次调用就能把 provider 配额和进程打光。
# 递归委派没有实际收益（父就在上面），所以直接从可授权集合里剔掉。
_NEVER_DELEGATED = frozenset({"spawn_agent"})


class SubAgentOutcome(BaseModel):
    """一次子 agent 运行的结果。父侧据此回填模型 + 落审计事件。"""

    agent_id: str
    text: str
    turns: int = 0
    ok: bool = True


class SubagentRunner:
    """把 `run_subagent` 收敛成可注入的执行体（plan/03 §8）。

    构造期接受父 loop 的资源引用（provider/registry/model 与父的身份/权限），
    运行期按 `SubAgentSpec` 隔离出子 loop。父 loop 每请求新建，天然作用域一致。

    注意这里**不再持有 SessionStore**：子 agent 不写库（见模块文档）。
    """

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        default_model: str,
        parent_session_id,
        *,
        tenant_id: str | None = None,
        granted_scopes: list[str] | None = None,
        parent_tools: list[str] | None = None,
    ):
        self._provider = provider
        self._registry = registry
        self._default_model = default_model
        self._parent_session_id = parent_session_id
        self._tenant_id = tenant_id or ""
        # 父的授权原样下传：子 agent 权限 ⊆ 父权限
        self._granted_scopes = list(granted_scopes or [])
        # 父这轮暴露给模型的工具名集合；None 表示注册表全集。子的 allowed_tools
        # 要与它求交，防止模型自己写出一个父都没有的工具名来提权。
        self._parent_tools = parent_tools

    def allowed_tools(self, requested: list[str]) -> list[str]:
        """requested ∩ 父工具集 − 黑名单，保持 requested 的顺序。

        求交而不是照抄：模型完全可以在 allowed_tools 里写一个父这轮没被授权的
        工具名，registry 里却有——那就成了「子 agent 比父 agent 权限大」。
        """
        parent = set(self._parent_tools if self._parent_tools is not None else self._registry.names())
        return [
            t for t in requested if t in parent and t not in _NEVER_DELEGATED
        ]

    async def run(self, spec: SubAgentSpec) -> SubAgentOutcome:
        """跑一个子 agent。任何异常都收敛成异常消息回给父，不把父带崩。"""
        child_agent_id = f"sub-{uuid4().hex[:8]}"
        model = spec.model or self._default_model
        system = spec.system_prompt or _DEFAULT_SYSTEM
        # `allowed_tools` 替换（非合并），未指定则空集
        tools_schema = self._registry.to_openai_schema(spec.allowed_tools)

        # 子 loop 的消息状态完全在内存里，不落父 DAG
        messages: list[LLMMessage] = [LLMMessage(role=Role.user, content=spec.task)]

        final_text = ""
        text_acc = ""  # max_turns 用尽时要回传最后一条 assistant 文本，先给个初值
        ok = True
        turn = 0
        try:
            while turn < spec.max_turns:
                turn += 1
                request = LLMRequest(
                    model=model,
                    system=system,
                    messages=messages,
                    max_tokens=spec.max_tokens,
                    tools=tools_schema,
                )
                text_acc, tool_calls, finish = await self._one_llm_call(request)

                # 无论是否调工具，先把 assistant 消息压进内存链
                messages.append(
                    LLMMessage(
                        role=Role.assistant, content=text_acc, tool_calls=tool_calls
                    )
                )

                # 无 tool_call → 自然结束
                if finish != "tool_use" or not tool_calls:
                    final_text = text_acc
                    break

                # 执行工具（读写分批复用父的 executor；子 agent 也享受 fan-out）
                results = await self._execute_tools(tool_calls, child_agent_id)
                # 回填工具结果（一条 tool 消息承载所有结果，与父 loop 惯例一致）
                messages.append(
                    LLMMessage(
                        role=Role.tool,
                        tool_results=[
                            _result_to_message(c, r) for c, r in zip(tool_calls, results)
                        ],
                    )
                )
            else:
                # 未 break → max_turns 用尽，把最后一条 assistant 文本回传
                final_text = text_acc or "[subagent] max_turns reached without a final answer"
        except ProviderError as e:
            final_text = f"[subagent-error] provider: {e}"
            ok = False
        except Exception as e:  # noqa: BLE001  子 agent 崩溃不应把父带崩
            log.warning("subagent_crashed", agent_id=child_agent_id, error=str(e))
            final_text = f"[subagent-error] {e}"
            ok = False

        log.info("subagent_finished", agent_id=child_agent_id, turns=turn, ok=ok)
        return SubAgentOutcome(agent_id=child_agent_id, text=final_text, turns=turn, ok=ok)

    # —— 内部辅助 ——

    async def _one_llm_call(self, request: LLMRequest):
        """跑一次 LLM 流式调用，累积成 (text, tool_calls, finish_reason)。"""
        text_acc = ""
        tool_calls: list[ToolCall] = []
        finish = "stop"
        async for chunk in self._provider.stream(request):
            if chunk.type == "text" and chunk.text:
                text_acc += chunk.text
            elif chunk.type == "tool_call" and chunk.tool_call:
                tool_calls.append(chunk.tool_call)
            elif chunk.type == "finish":
                finish = chunk.finish_reason or "stop"
        return text_acc, tool_calls, finish

    async def _execute_tools(
        self, tool_calls: list[ToolCall], child_agent_id: str
    ) -> list[ToolResult]:
        """复用父的读写分批执行器。子 agent 独立 agent_id 便于审计。

        ctx 带上父的 tenant_id 与 granted_scopes：不带等于让子 agent 在权限检查
        里「没有主体」，那正是提权口子（见 mcp/proxy_tool 的 scope 默认拒绝）。
        """
        ctx = ToolContext(
            tenant_id=self._tenant_id,
            session_id=str(self._parent_session_id),
            agent_id=child_agent_id,
            granted_scopes=self._granted_scopes,
        )
        return await execute_batched(
            tool_calls,
            self._registry,
            ctx,
            # 子 agent 不允许有共享上下文副作用应用器：不接父的 applier，
            # 让工具的 mutation 停留在 result 上不被落库。这符合「隔离」的意图。
            apply_mutation=None,
        )


def _result_to_message(call: ToolCall, result: ToolResult):
    """把 ToolResult 拉成 ToolResultMessage 供子 loop 下一轮消息包裹。"""
    from app.domain.llm import ToolResultMessage

    return ToolResultMessage(
        tool_call_id=call.id,
        content=_stringify(result.content),
        is_error=not result.ok,
    )


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
