"""评测题的数据契约（YAML → Pydantic）。

一题一文件，文件名即题号。用 Pydantic 而不是裸 dict 的理由：题本会长到几十个
文件，字段拼错（`stop_reson`）必须在**加载期**就炸，而不是等跑到断言时静默
跳过——静默跳过的评测集会给出虚高的通过率，那比没有评测集更糟。

`extra="forbid"` 是这条防线的关键：多写一个没人读的字段等于一条不生效的断言。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Dimension(str, Enum):
    """能力面。值同时是 cases/ 下的子目录名。"""

    tools = "tools"
    compact = "compact"
    memory = "memory"
    convo = "convo"
    subagent = "subagent"
    safety = "safety"
    resilience = "resilience"


class Layer(str, Enum):
    """难度层。目标配比 basic:twist:hard ≈ 5:3:2。"""

    basic = "basic"
    twist = "twist"
    hard = "hard"


class Path_(str, Enum):
    """跑法：非流式聚合响应，或 SSE 流式。"""

    messages = "messages"
    stream = "messages/stream"


class Invariant(str, Enum):
    """全局不变式（默认全开，可按题关掉）。实现见 evals/invariants.py。"""

    no_orphan_tool_use = "no_orphan_tool_use"
    dag_parent_chain = "dag_parent_chain"
    zero_side_effect_on_4xx = "zero_side_effect_on_4xx"
    stop_reason_in_enum = "stop_reason_in_enum"


DEFAULT_INVARIANTS: tuple[Invariant, ...] = (
    Invariant.no_orphan_tool_use,
    Invariant.dag_parent_chain,
    Invariant.zero_side_effect_on_4xx,
    Invariant.stop_reason_in_enum,
)


class ToolExpect(BaseModel):
    """对一次工具调用的期望。

    `arguments` 是**子集匹配**：只校验列出的键,不要求逐字全等。理由是参数里
    可能有 executor 补的字段(如 tool_call_id),题本不该被实现细节绑死。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] | None = None
    ok: bool | None = None
    # 对 tool_result 内容的子串断言。为什么需要它：MockProvider 的回声只回用户
    # 原文（剥掉 [[tool:...]] 指令），**不回工具结果**——所以「工具真的干活了」
    # 只能在结果块上断言，写成 reply_contains 会是假通过。
    result_contains: list[str] = Field(default_factory=list)
    # 结果里的错误码（优雅失败题用）。工具错误结果的形状由各工具决定，这里同样
    # 按子串匹配，避免把题本绑死在某个 JSON 键名上。
    error_contains: list[str] = Field(default_factory=list)
    # 结果里**不该**出现的子串。安全题用：证明沙箱外的字节没进上下文。
    result_excludes: list[str] = Field(default_factory=list)


class Expect(BaseModel):
    """判分标准。**先写死,再跑**——跑完再改这里等于没有评测。"""

    model_config = ConfigDict(extra="forbid")

    # —— 结果层 ——
    http_status: int = 200
    stop_reason: str | None = None

    # —— 过程层(主指标)——
    # tools 为 None 表示不校验;空列表表示「必须一次工具都没调」,两者不同。
    tools: list[ToolExpect] | None = None
    # 工具调用总次数上界。过程层的「不许空转」就靠它。
    max_tool_calls: int | None = None
    # 投影后的消息条数(user/assistant/tool 交替)。snapshot 不进投影,
    # 所以这个数字能反向证明「引用正文只出现一次」。
    projection_len: int | None = None

    # —— 文本层(弱断言,只做包含检查)——
    reply_contains: list[str] = Field(default_factory=list)
    reply_excludes: list[str] = Field(default_factory=list)

    # —— 事件层 ——
    # 期望出现的事件类型(SSE 帧的 event: 名或 Event.type),如 steered / compact。
    events_include: list[str] = Field(default_factory=list)
    # 期望落库的事件 kind 计数,如 {"snapshot": 1}。
    event_kind_counts: dict[str, int] | None = None

    # —— 副作用层 ——
    # session.meta["notes"] 应包含的文本。写工具（note_append）的副作用不落 DAG
    # 事件而是落 session.meta，所以「写工具真的写了」只能在这里断言。
    session_notes: list[str] = Field(default_factory=list)
    # session.meta["notes"] 里**不该**出现的文本。能力隔离题用：子 agent 的写
    # 不该落到父上下文。
    session_notes_exclude: list[str] = Field(default_factory=list)

    # —— 控制面层 ——
    # 响应里 reference_ids 至少几个（引用题）。不钉精确数：一条消息可以带多个引用，
    # 钉数量会让题本绑死在 fixture 的条数上。
    min_reference_ids: int | None = None
    # 被抢占的旧 run 是否被写了 superseded 取消标记（double-texting interrupt 题）。
    preempted_run_cancelled: bool = False
    # 引导内容应出现在 steered 事件或落库历史里（引导不丢）。
    steered_contains: list[str] = Field(default_factory=list)

    # —— 成本层 ——
    # 响应 usage 的 output_tokens 下界。委派题用：子 agent 的用量必须冒泡到父
    # （_absorb_subagent_usage），否则一次请求的真实成本对调用方不可见。
    min_output_tokens: int | None = None

    # —— 压缩层（D2）——
    # done 帧的 retriable 字段。客户端按它决定要不要重试，值错了会让客户端对
    # 不可恢复的失败死循环重试（compact_failed）或对可恢复的失败直接放弃。
    retriable: bool | None = None
    # compact 事件的 layer 必须是这个值。压缩的核心行为是**选层**：有可回收的
    # 工具结果时该走 microcompact（省、可逆、保缓存），没有才升级到全量摘要。
    # 只断言「压缩发生了」而不看层，选层逻辑反了也是绿的。
    compact_layer: str | None = None
    # 主链上被占位化的工具结果块数下界（microcompact 真的回收了内容）。
    # 只看 compact 事件的 freed_tokens 不够：那是压缩函数自己报的数。
    reclaimed_results_min: int | None = None
    # microcompact 不得重排父指针（prompt cache 语义：改父指针 = 缓存前缀失效）。
    # 比对被测那一轮前后「已存在事件」的 parent_id 映射,新增事件不算。
    parents_unchanged: bool = False
    # compact_boundary 事件必须 parent_id is None（切断前史）且 logical_parent_id
    # 非空（真实前史仍可审计/回放）。这两个指针的组合就是全量摘要的正确性本身。
    boundary_cuts_history: bool = False

    # —— 记忆层（D3）——
    # 被测会话 user scope（external_user）记忆里应出现的子串。remember 是写工具，
    # 但 Mock 不回显工具结果、仓库也没有记忆读取 API，所以「记住了」只能直接查
    # DbMemoryStore（按会话自身 tenant + external_user）。这是「写进去了」唯一的观测点。
    memory_user_scope_contains: list[str] = Field(default_factory=list)
    # seed 各自 external_user scope 里应出现的子串。隔离题（D3-H01）的非平凡性前提：
    # 先钉「A 确实写过」，否则 recalled_prompt_excludes 会因为没东西可泄漏而假绿。
    seed_scope_contains: list[str] = Field(default_factory=list)
    # 被测那一轮 provider 实际收到的 system prompt 里应出现的子串。召回只注入系统
    # 提示（PromptComposer → <memory> 块），Mock 不回显它，所以「召回是否发生」只能
    # 在「喂给模型的 system」这个边界上看。被测会话新开、无历史 → 命中即证明是
    # 跨会话「记忆」而非「上下文」。
    recalled_prompt_contains: list[str] = Field(default_factory=list)
    # 被测那一轮 system prompt 里**不该**出现的子串。跨用户隔离题（D3-H01）用：
    # 用户 B 的召回不得带出用户 A 的记忆（scope_key=external_user 级隔离）。
    recalled_prompt_excludes: list[str] = Field(default_factory=list)


class CompactCfg(BaseModel):
    """压缩题的运行时改装（D2 专用）。

    为什么必须有它：Mock 的上下文窗口是 200k（`_MODEL_WINDOWS["mock"]`），
    默认压缩阈值高得灌不到——靠堆消息去触发压缩，一道题要发几百轮，还会把
    评测跑成分钟级。所以改装阈值，让「压缩触发」这件事变成可控前提，
    而被测对象（选层、边界形状、熔断）仍然是产品代码。

    做成**具名字段**而不是通用 monkeypatch 钩子：题本里能任意打补丁的评测集，
    读者无法判断一道题测的是产品还是补丁。这里三个字段各自对应一个明确的前提。
    """

    model_config = ConfigDict(extra="forbid")

    # 把 agent_loop 里的 compact_threshold 换成常量。**只在铺垫轮跑完之后生效**：
    # 铺垫期就改会让每一轮铺垫自己触发压缩，被测那一轮的前提就不是题本写的那个。
    threshold_tokens: int | None = None
    # 让摘要模型调用抛错 → CompactionError。熔断题（连续失败到上限）需要它。
    # 不用「让历史里带 [[tool:]] 指令把摘要挤成空串」那种巧劲：那是靠 Mock 的
    # 解析细节间接制造失败，读者看不出题在测什么。
    fail_summarizer: bool = False
    # 允许连续几轮都产出工具调用。Mock 原本「见到工具结果就收尾」→ 一次 run 最多
    # 2 轮，而熔断阈值是 3 次连续压缩失败，2 轮永远够不到。
    tool_turns: int = 1


class ResilienceCfg(BaseModel):
    """降级题的运行时改装（D7 专用）。

    overload_models 里的 model 名，provider 一律抛 ProviderOverloaded（429/503 的
    归一化），触发 Loop 的模型降级链。做成**具名字段**而不是通用故障注入钩子，理由
    同 CompactCfg：题本里能任意打补丁的评测集，读者无法判断一道题测的是产品还是补丁。
    这里一个字段对应一个明确前提——「这些 model 过载」。

    只让主模型过载 → 降级到备用成功（T01）；主模型和所有 fallback 都过载 → 降级链
    耗尽 → provider_unavailable（H01）。哪个是主、哪些是备，由 case.env 的
    DEFAULT_MODEL / FALLBACK_MODELS 指定，与这里的集合对应上。
    """

    model_config = ConfigDict(extra="forbid")

    # 这些 model 名一律过载。空列表 = 不注入故障（等于没设 resilience）。
    overload_models: list[str] = Field(default_factory=list)


class Action(BaseModel):
    """跑题期间的额外动作(见设计 §5.3)。

    这些是 MockProvider 脚本语法表达不了的时序/并发行为:占锁、中途取消、
    中途引导。`at_ms` 是相对发消息时刻的延迟。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str  # hold_lock | cancel | steer | set_policy | preempt
    at_ms: int = 0
    # steer 用
    text: str | None = None
    mode: str | None = None
    # hold_lock 用:占多久
    hold_ms: int | None = None
    # set_policy 用
    policy: str | None = None


class Probe(BaseModel):
    """只打一次控制面接口、只看状态码的题。

    为什么单独一类：「无活跃 run 时取消返回 404」这种题的被测对象是接口契约，
    不是一轮对话。硬塞进 input/expect 会逼着题本发一条没人关心的消息，
    然后在一堆对话断言里藏一条状态码断言。
    """

    model_config = ConfigDict(extra="forbid")

    method: str = "POST"
    # 支持 {session_id} 占位符
    path: str
    body: dict[str, Any] | None = None


class Confirm(BaseModel):
    """被测那一轮挂起后的确认动作（dangerous 工具题用）。

    与 followup 分开：确认走的是 /confirmations 而不是 /messages，且它的返回体
    本身就是「恢复后的那一轮」——语义上是同一轮的续，不是新一轮。
    """

    model_config = ConfigDict(extra="forbid")

    # true = 批准；false = 拒绝（reject_all）
    approved: bool = False
    http_status: int = 200
    stop_reason: str | None = None
    # 恢复后工具结果里应出现的子串（拒绝时应能看出「用户拒绝」）
    result_contains: list[str] = Field(default_factory=list)


class Followup(BaseModel):
    """被测那一轮之后再发的一条消息。

    存在的理由是一类断言只能靠后续轮次证明：「取消之后会话没报废」、「锁释放后
    第三句能过」。这些不是新的一道题——它们是同一道题的收尾条件，拆成两题会丢掉
    「同一个 session」这个关键前提。
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    http_status: int = 200
    stop_reason: str | None = None


class SeedTurn(BaseModel):
    """被测那一轮之前，在**另一个会话**里先跑的一条写入消息（通常是 remember）。

    与 preamble 的区别：preamble 是同一会话的前几轮（测多轮上下文）；seed 另开
    会话、可指定自己的 external_user（测跨会话 / 跨用户的长期记忆）。记忆的意义
    正是「换个会话仍在」——所以召回题的写入必须发生在别的会话，否则测的是对话
    历史，不是记忆。
    """

    model_config = ConfigDict(extra="forbid")

    external_user: str
    content: str


class Case(BaseModel):
    """一道评测题。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    dimension: Dimension
    layer: Layer
    title: str
    # 并发/时序题设 5;确定性题 1 次足够(Mock 无随机性,跑 5 次是浪费)。
    repeats: int = 1
    path: Path_ = Path_.messages

    # 被测会话的 external_user（用户标识）。默认 eval-{id}。记忆题显式指定：召回轮
    # （新会话）要与 seed 写入轮用**相同** external_user 才能跨会话召回（T01）；
    # 隔离题则故意用**不同**的 external_user（H01）。
    external_user: str | None = None

    # 给 MockProvider 的每片延迟(毫秒)。默认 0 = 尽可能快。
    # 取消/引导题必须 > 0：零延迟下 run 在 HTTP 请求返回前就结束了，取消打在
    # 一个已经结束的 run 上——那样的题永远绿，且什么都没测到。
    mock_delay_ms: int = 0

    # 跑这题时临时覆盖的环境变量（get_settings 是 lru_cache 单例，runner 改完
    # 环境变量会 cache_clear）。用于闸门题：把 SUBAGENT_MAX_PER_RUN 调到 1 才能
    # 在两次 spawn 内测到 fan_out_exceeded，而不是灌 7 次 spawn 去凑默认上限。
    # 跑完必须还原——否则一道题的配置会污染后面所有题。
    env: dict[str, str] = Field(default_factory=dict)

    # 被测那一轮之前先跑完的消息（只要求成功，不判分）。多轮记忆/多轮上下文题用。
    # 故意做成「纯文本列表」而不是完整的 Case 列表：铺垫轮如果也能带断言，题本就会
    # 变成脚本语言，而判分标准分散在多处的评测集是不可读的。
    preamble: list[str] = Field(default_factory=list)

    # 被测那一轮之前，在**独立会话**里先跑的写入轮（每条可带自己的 external_user）。
    # 长期记忆题用：召回轮要新开会话才能证明「跨会话记住」，隔离题要两个不同用户。
    seed: list[SeedTurn] = Field(default_factory=list)

    # input 为 None → 不发消息，只打控制面接口（probe）。D4-B01/B02 这类
    # 「无活跃 run 时取消返回什么」的题根本不需要一轮对话。
    input: dict[str, Any] | None = None
    # 控制面探针：直接打一个接口并只校验状态码。method/path 里的 {session_id}
    # 会被替换成本题新建的会话 id。
    probe: Probe | None = None
    # 压缩题的运行时改装（阈值 / 摘要失败 / 多轮工具）。见 CompactCfg。
    compact: CompactCfg | None = None
    # 降级题的运行时改装（哪些 model 过载）。见 ResilienceCfg。
    resilience: ResilienceCfg | None = None
    expect: Expect
    # 挂起确认后的动作。执行顺序：input → confirm → followup。
    confirm: Confirm | None = None
    followup: Followup | None = None
    actions: list[Action] = Field(default_factory=list)
    invariants: list[Invariant] = Field(default_factory=lambda: list(DEFAULT_INVARIANTS))

    # 题注:来源、已知边界、为什么这么断言。会印进报告,不是给人看完就丢的注释。
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Case:
        if self.input is None and self.probe is None:
            raise ValueError(f"{self.id}: 需要 input 或 probe 之一")
        if self.input is not None and "content" not in self.input:
            raise ValueError(f"{self.id}: input.content is required")
        if self.repeats < 1:
            raise ValueError(f"{self.id}: repeats must be >= 1")
        # retriable 与 compact 帧只在 SSE 上可见：非流式响应体（MessageResponse）
        # 没有 retriable 字段，compact 事件也不进聚合结果。在非流式路径上写这两条
        # 断言 → 读不到值 → 永远绿。
        if self.path is not Path_.stream:
            if self.expect.retriable is not None:
                raise ValueError(
                    f"{self.id}: expect.retriable 需要 path=messages/stream"
                    "（非流式响应体里没有 retriable 字段）"
                )
            if self.expect.compact_layer is not None:
                raise ValueError(
                    f"{self.id}: expect.compact_layer 需要 path=messages/stream"
                    "（compact 事件不进非流式聚合响应）"
                )
        if self.expect.compact_layer is not None and self.compact is None:
            raise ValueError(
                f"{self.id}: 断言了 compact_layer 却没设 compact.threshold_tokens，"
                "Mock 窗口 200k 下压缩根本不会触发"
            )
        if any(a.kind == "cancel" or a.kind == "steer" for a in self.actions):
            if self.path is not Path_.stream:
                raise ValueError(
                    f"{self.id}: cancel/steer 需要 path=messages/stream"
                    "（run:current 只在流式路径写入，非流式路径取消是静默 no-op）"
                )
            if self.mock_delay_ms <= 0:
                raise ValueError(
                    f"{self.id}: cancel/steer 题必须设 mock_delay_ms > 0，"
                    "否则 run 在取消到达前就结束了，题目永远绿"
                )
        if self.expect.seed_scope_contains and not self.seed:
            raise ValueError(
                f"{self.id}: seed_scope_contains 需要 seed"
                "（没有写入轮就没有可查的 seed scope，断言会假绿）"
            )
        return self


def load_case(path: str | Path) -> Case:
    """读一个 YAML 题文件。字段拼错在这里就炸。"""
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level must be a mapping")
    return Case(**raw)


def load_all_cases(base: str | Path | None = None) -> list[Case]:
    """加载 cases/ 下全部题,按 id 排序。

    排序而不是按文件系统顺序:报告要稳定,不能因为换台机器就重排行。
    """
    root = Path(base) if base else Path(__file__).parent / "cases"
    cases = [load_case(p) for p in sorted(root.rglob("*.yaml"))]
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    return sorted(cases, key=lambda c: c.id)
