"""子 Agent 领域模型（plan/03 §8、plan/12 §5-§6）。

子 agent 是「LLM 层面的函数调用」：另开一个不落父 DAG 的消息列表，在里面把子任务做完，
然后**连同整个中间过程一起丢掉**，只把返回值交回父上下文。与压缩的关系见 plan/12 §3
——压缩是垃圾回收（事后清理已进主堆的垃圾），子 agent 是栈帧回收（垃圾从不进主堆）。

本模块只放纯数据与纯逻辑：预算、运行上下文、返回值、审计 trace。带 asyncio 的闸门
（并发槽、事件出口）放在 `orchestration/fleet.py`——domain 层不引运行期设施，也便于
CI 对 `app/domain` 的 mypy 硬门禁。

两条不变量刻意写进类型而不是写进注释：

- **能力只减不增**：`AgentRunContext` 只提供 `child()` 一条派生途径，内部强制与自身求交。
  没有别的构造子上下文的方式，于是「子 agent 凭空拿到父所没有的 scope」在结构上不可能。
  这修的是一个实测缺陷：阶段 7 的子 ToolContext 是空 scope。MCP 代理已改为默认
  拒绝空 scope（见 `mcp/proxy_tool`）；清空不再提权，但会把子 agent 的 MCP 工具
  全部废掉。无论哪一种，父到子都必须继承再求交，不能清空（plan/12 §4.5）。
- **预算是引用语义，深度是值语义**：`TokenBudget` 由整棵树共享，任何一层扣减全树立即
  可见；`depth` 每层一份。两者混进同一个可变对象，就会出现「子 agent 改了父的 depth」。
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.domain.llm import Usage

# —— 命名退出原因（与 plan/03 §2 的 stop_reason 同构：命名转移才可单测、可观测）——
SUB_STOP_COMPLETED = "subagent_completed"
SUB_STOP_MAX_TURNS = "subagent_max_turns"
SUB_STOP_BUDGET = "subagent_budget"
SUB_STOP_DEPTH = "subagent_depth"
SUB_STOP_CONTEXT_OVERFLOW = "subagent_context_overflow"
SUB_STOP_ERROR = "subagent_error"

# —— 派发被闸门拒绝的原因（直接作为 metrics label，故用稳定的短标识）——
DENY_DEPTH = "depth_exceeded"
DENY_FAN_OUT = "fan_out_exceeded"
DENY_BUDGET = "budget_exhausted"
DENY_DISABLED = "subagent_disabled"

# ContextMutation 的 kind：子 agent 的审计 trace 由父 loop 的 applier 落库，
# 不由子 agent 自己写 DB（plan/12 §10.1）。放在 domain 供 runner/tool/loop 三方共用。
MUTATION_SUBAGENT_TRACE = "subagent_trace"


@dataclass
class TokenBudget:
    """整棵委派树共享的 token 预算（plan/12 §5.1）。

    为什么必须有：单 agent 的花费被上下文窗口天然封顶，多 agent 拆了这个顶——每个子
    agent 都是一个全新窗口。10 个子 agent × 6 轮 × 每轮 2 万 = 120 万 token，而这一切
    发生在**一次** HTTP 请求内，租户限流只数到 1。
    """

    limit: int
    spent: int = 0

    def charge(self, usage: Usage) -> None:
        self.spent += usage.input_tokens + usage.output_tokens

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def exhausted(self) -> bool:
        return self.spent >= self.limit


class AgentRunContext(BaseModel):
    """一个节点在委派树中的位置与能力（plan/12 §5.2、§6）。值语义，每层一份。

    `granted_scopes` 必须从父完整继承再求交（`child()` 是唯一派生途径）。MCP 代理
    已经改成默认拒绝空 scope（见 `mcp/proxy_tool.check_permissions`）：清空子的
    scope 不再是「未注入 → 不设卡」，而是「没有任何授权 → 全部 MCP 工具被拒」。
    两种语义下，父到子都必须是继承而非清空——清空要么提权、要么把子 agent 废掉。
    """

    depth: int = 0
    agent_id: str = ""                  # 本节点标识；根节点为父 loop 的模型名
    parent_agent_id: str | None = None
    tenant_id: str = ""
    trace_id: str = ""
    granted_scopes: list[str] = Field(default_factory=list)

    def child(self, *, agent_id: str, scopes: list[str] | None = None) -> AgentRunContext:
        """派生子上下文：深度 +1、能力只减不增。

        scopes 显式给定时与自身求交（请求 `mcp:b` 而父只有 `mcp:a` → 交集为空），
        不给定则原样继承。这是唯一的派生途径，所以放大在结构上不可能。
        """
        narrowed = (
            [s for s in scopes if s in self.granted_scopes]
            if scopes is not None
            else list(self.granted_scopes)
        )
        return AgentRunContext(
            depth=self.depth + 1,
            agent_id=agent_id,
            parent_agent_id=self.agent_id or None,
            tenant_id=self.tenant_id,
            trace_id=self.trace_id,
            granted_scopes=narrowed,
        )


class SubAgentTrace(BaseModel):
    """一个子 agent 的审计留痕（plan/12 §10.1）。

    由 `spawn_agent` 通过 ContextMutation 交回父 loop 落库，而非子 agent 自己写 DB
    ——后者会让 N 个并发子 agent 同时用同一个 AsyncSession（实测缺陷，plan/12 §4.5）。
    `children` 让嵌套层的 trace 逐层冒泡，否则子层 `apply_mutation=None` 会把孙 agent
    的审计整段丢掉。
    """

    agent_id: str
    agent_type: str = "adhoc"
    depth: int = 0
    task: str = ""
    result_digest: str = ""             # 结果摘要（截断），不放全文避免撑爆父 DAG
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str = SUB_STOP_COMPLETED
    turns: int = 0
    duration_ms: int = 0
    children: list[SubAgentTrace] = Field(default_factory=list)

    def flatten(self) -> list[SubAgentTrace]:
        """先序展开整棵子树，供父 loop 按顺序逐条落 sidechain 事件。"""
        out = [self]
        for c in self.children:
            out.extend(c.flatten())
        return out

    def total_usage(self) -> Usage:
        """整棵子树的用量合计——父只 charge 一次就能拿到全部成本。"""
        total = self.usage
        for c in self.children:
            total = total + c.total_usage()
        return total


class SubAgentResult(BaseModel):
    """子 agent 的返回值（plan/12 §6）。替换阶段 7 的裸 `str`。

    裸 str 的两个问题（plan/12 §4.2）：父要从散文里抠数字、抠错无人知晓；长度不可控，
    三份各 2000 字又把父上下文顶回去了——为省上下文才委派，结果没省下来。
    `structured` 在 M2 接入 `output_schema` 后承载校验过的结构，M1 先立字段。
    """

    agent_id: str
    agent_type: str = "adhoc"
    depth: int = 0
    text: str = ""
    structured: dict | None = None
    usage: Usage = Field(default_factory=Usage)      # 含整棵子树
    stop_reason: str = SUB_STOP_COMPLETED
    turns: int = 0
    trace: SubAgentTrace


class SubAgentSpec(BaseModel):
    """一次子 agent 派发的配置（plan/03 §8）。

    `allowed_tools` 是**替换而非合并**——独立收紧权限，防止子 agent 拿到父的全部工具。
    M1 保持「由模型在参数里给出」的现状；M2 会换成清单声明的 `agent_type`（plan/12 §7），
    因为让模型决定能力授予，等于把授权交给任何能影响模型输入的人（召回记忆、MCP 返回文本）。
    """

    task: str                                      # 交给子 agent 的任务描述
    allowed_tools: list[str] = Field(default_factory=list)  # 替换（非合并）父工具集
    model: str | None = None                       # 可用更便宜的模型；None 复用父模型
    # 下界 1：max_turns=0 会让子 loop 一次 LLM 都不调就返回兜底串，那不是语义，
    # 是参数错误，应在构造期炸掉。上界 32：防模型自己写一个离谱的数把一次子任务变成压测。
    max_turns: int = Field(default=6, ge=1, le=32)
    max_tokens: int = Field(default=2048, ge=1)
    system_prompt: str | None = None               # 子 agent 独立 system；None 用默认
    agent_type: str = "adhoc"                      # 审计/指标 label；M2 起指向清单名
    # 子 agent 通常只读 → 可 fan-out 并行。若需写侧作用，spawn_agent 的 spec
    # 也可标 is_read_only=False；此字段用于日志/审计，实际串并行由工具属性决定。
    read_only: bool = True

