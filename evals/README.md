# AgentGate 运行时评测集

> 这个目录是仓库的**测量仪器**，不是运行时的一部分——所以它不在 `app/` 里。
> 设计文档：`docs/superpowers/specs/2026-09-12-runtime-eval-set-design.md`。

## 一句话

同一批固定任务，在 Mock provider 下，编排、工具、上下文、取消/引导/引用、子 Agent
闸门、安全与韧性是否**按契约发生**——以及改一处之后，同一批任务是变好还是变差。

## 明确不评

| 不评 | 为什么 |
|---|---|
| 最终自然语言好不好看 | 那是模型的事，不是本仓库的代码 |
| 召回率 / 幻觉率 | 同上；本仓库只是网关 |
| 真实 provider 的延迟与配额 | 评测 provider 固定 Mock，为的是可复现、离线、秒级 |
| MCP 真实 server 端到端 | 已有 `scripts/mcp_smoke.py` 与单测；评测不把外部进程当依赖 |

含糊过去这条边界是减分项。面试里被问「你的评测测什么」，答案的第一句应该是
**「测运行时行为，不测答案质量」**。

## 怎么跑

前置与 e2e 相同：PostgreSQL + Redis 起着，且 schema 已迁移。

```bash
docker compose up -d postgres redis
.venv/Scripts/python.exe -m alembic upgrade head        # 首次或迁移后

# 跑一题（开发时最常用）
.venv/Scripts/python.exe -m evals.runner --case D1-B01

# 跑一个能力面
.venv/Scripts/python.exe -m evals.runner --dimension convo

# 跑全集 + 出报告（写 evals/reports/latest.md 与 raw-*.json）
.venv/Scripts/python.exe -m evals.runner --all --report
```

`--dimension` 的取值就是 `cases/` 下的子目录名：`tools` / `compact` / `memory` /
`convo` / `subagent` / `safety` / `resilience`。

消融对比（同一批题在几个真实旋钮下各跑一遍，出 `evals/reports/ablation.md`）：

```bash
.venv/Scripts/python.exe -m evals.ablation            # A–D 四列，约 4 分钟
.venv/Scripts/python.exe -m evals.ablation --arms A B  # 只跑其中两列
```

| 方案 | 怎么切 | 想证明什么 |
|---|---|---|
| A 基线 | 默认 settings + Mock | 主指标水位 |
| B 关子 Agent | `SUBAGENT_ENABLED=false` | D5 变明确拒绝，D1 不受影响 |
| C 压缩更激进 | `EVAL_FORCE_COMPACT_THRESHOLD=400` | D2 触发率上升，D1 不该掉 |
| D 双发 reject | `EVAL_FORCE_POLICY=reject` | D4-T04 从抢占变 409，其余不变 |

E 列（真模型换掉 Mock）第一版不做：Mock 下的数字与真模型下的数字不可比，要单独
成表，混在一起会误导。

`EVAL_FORCE_*` 两个变量只被 `evals/` 读，产品代码里不存在——它们是**评测夹具的
兜底开关**，且题本自己声明的前提（`compact.threshold_tokens`、`set_policy`）优先，
消融不会盖掉一道题自己的前提。

**这张表的读法**：不看「哪一列分高」——B/C/D 都是刻意把产品调坏或调偏，分低是预期。
要看的是**变化是否落在该变的维度上**。实测：B 只崩 D5（5/5 → 0/5，错误是明确的
`permission_denied`），D 只崩 D4-T04（200 抢占 → 409），C 零翻面。若关子 Agent 把 D1
也带崩了，那才是这张表要抓的东西。

一个真实的读表陷阱：D4-T01（运行中取消）在 B、D 两列都翻了面，但它是 `repeats: 5`
的时序题，实测 4/5——那是**竞态抖动**，不是旋钮效应。表里会标出 `n/5` 提醒这一点。

退出码**恒为 0**，即使有题失败。理由：评测的「完成率 94%」是合法结果，不是 CI 红
（设计 §9.3）。需要门禁语义的调用方自己去读报告。

评测集**不进 CI 必过路径**（设计 §9.2）。第二版再考虑 `workflow_dispatch` 或 nightly；
放进 PR 门禁的前提是全集 < 60s 且零 flake。

## 报告怎么读

`--report` 产出两份：

- `evals/reports/latest.md`——**进库**。按能力面、按分层、按 `stop_reason` 分布出表，
  附未通过题的清单。人读的那份。
- `evals/reports/raw-*.json`——**不进库**（`.gitignore`）。原始数字，留作消融对比时做差。
  耗时受本机负载影响，进库只会制造无意义的 diff。

几个读法上的要点：

1. **没有「总分」。** 32 道题的算术平均没有意义——刁难层跑红是设计允许的结果，
   摊进一个百分比反而藏住了「哪一面弱」。弱在哪，看按能力面那两列。
2. **「轨迹正确率」就是每题的 `ok`。** 过程层断言（工具名序列、参数、步数上界、
   无孤儿 tool_use）都写在 YAML 的 `expect` 里，`ok` 本身即轨迹正确，不单列指标。
3. **刁难层不是 100% 是预期。** 那一层收的就是取消中途、越权、注入、闸门这类
   「优雅失败」题，其中一部分钉的是**已知产品缺口**（题注里写明）。
4. **允许、甚至鼓励难看的数字。** 敢报差的指标 + 讲清边界，比假满分可信。
   `ops/benchmark.md` 已经是这个文风，本报告沿用。

## 题本约定

一题一文件，`cases/{dimension}/{id}.yaml`，文件名即题号。字段全在 `evals/schema.py`。

- **`extra="forbid"`**：多写一个没人读的字段会**在加载期就炸**。静默跳过的断言
  等于一条不存在的断言，而虚高的通过率比没有评测集更糟。
- **断言先写死，再跑。** 跑完再改 `expect` 等于没有评测。「差不多算过」不算数。
- **禁止改已有题的 `expect` 来让报告变好看。** 真要改标准（产品语义变了），
  在 PR 里单独说明「黄金集漂移」，并标注该题从哪次 commit 起重算（设计 §8）。
- **题注（`note`）是题的一部分。** 写清：来源、为什么这么断言、**反证方式**
  （怎么改产品代码能让这题变红）、已知边界。会印进报告，不是看完就丢的注释。

### 分层（5:3:2）

| `layer` | 设计占比 | 语义 |
|---|---|---|
| `basic` | ~50% | 单轮 / 单工具 / 主路径走通 |
| `twist` | ~30% | 多工具批、多轮、压缩、子 Agent、引导/引用 |
| `hard` | ~20% | 取消中途、越权、注入、工具失败、超限闸门、优雅失败 |

**实际配比是 8 / 14 / 10，不是设计里的 16 / 10 / 6。** 铺题时按「这道题钉哪条不变式」
分层，结果偏向了 twist/hard。报告如实报出这个偏离，不回头给题重贴标签——分层是给
「难度分布」看的，改标签能让配比好看，但会让「哪一层真的在兜底」失真。要把 basic
补到设计比例，应该是**加题**（每个维度补几道主路径题），不是改现有题的 `layer`。

### 只改题本、不改产品的两个具名开关

题本里能任意打补丁的评测集，读者无法判断一道题测的是产品还是补丁。所以故障注入
只给**具名字段**，每个字段对应一个明确前提：

- `compact.threshold_tokens` / `compact.fail_summarizer` / `compact.tool_turns`（D2）
- `resilience.overload_models`（D7）

两者都只改**前提**，被测的仍是产品代码：压缩的选层/边界形状/熔断、过载后的降级链
全部走 `agent_loop` 的真实分支，一行产品代码没动。

## 目录

```
evals/
  README.md            # 本文件
  schema.py            # Case / Expect / ToolExpect（YAML → Pydantic）
  runner.py            # 读 YAML → ASGI → 断言 → CaseResult
  provider.py          # EvalMockProvider（嵌套脚本 + 三个故障开关）
  invariants.py        # 四条全局不变式
  report.py            # CaseResult → markdown 表 + 原始 json
  ablation.py          # A–D 四列消融对比 → ablation.md
  fixtures/            # 沙箱文件、dangerous 工具桩
  cases/{dimension}/   # 题本，一题一文件
  reports/             # latest.md / ablation.md 进库；raw-*.json 不进
```

## 与 `tests/` 的关系

| | `tests/` | `evals/`（本目录） |
|---|---|---|
| 粒度 | 函数 / 模块 / 单条 HTTP | 端到端任务（一条输入 → 完整 run） |
| 目的 | 防回归、钉住不变式 | 度量能力面、产出可对比数字 |
| 失败含义 | 代码坏了 | 这道任务没按契约完成（可以是已知边界） |
| 输出 | pytest 绿/红 | 完成率、稳定性、耗时、一张消融表 |
| CI | 必过门禁 | **第一版不进** |

两者互补，不互相替代。harness 复用 e2e 已有的装配（`create_app` + `ASGITransport`
+ MockProvider + PG/Redis），不另起一套 HTTP 客户端。

## 已知边界与坑（跑之前先读这段）

- **`_ensure_anon_tenant()` 是环境前提，不是后门。** `memory_item.tenant_id` 有外键
  指向 `tenant.id`，而 `session.tenant_id` 没有。`AUTH_REQUIRED=false` 下请求归到
  固定的 `_ANON_TENANT`（uuid int=0），于是建会话畅通、`remember` 落库撞外键。
  真实部署里每个租户都有 tenant 行（签发 API Key 时一并建）。runner 启动时幂等补
  这一行，补完之后跑的仍是产品那条 `_apply_remember → DbMemoryStore` 的完整路径。
- **单题有 60s 上限。** 超时收成失败并记「会话锁或压缩标记未释放？」，不往外冒——
  一道题炸不该拖垮后面 31 道。
- **熔断状态跨题共享（Redis 持久化）。** `CircuitBreaker` 的计数按 provider 名存在
  Redis 里，runner **不**按题 flush。当前题本的失败注入量（D7-H01 两次）远低于
  `fail_threshold=5`，且同一 run 里后续成功轮会 `on_success` 归零，所以互不干扰。
  **隐患**：单独反复只跑一道只失败不成功的题，计数会向 5 累积并改变行为。验证前
  对评测 Redis 做一次 flush 即可彻底规避。
- **D2-H01 当前是红的，且这是有意保留的记录**（题注里写了根因）：熔断路径上异步
  生成器的 `finally`（清 `session.active_compaction`）不在 `yield ..., True` 之后同步
  执行，而是等 GC 回收生成器时才跑——那时请求作用域的 DB session 已关闭，标记被
  留在 `"auto_compact"` 上，导致**收尾轮挂住不返回**（20s 判红）。修产品是单独一条
  PR 的事；在那之前本题以红色形式记录缺口，而不是改松 `expect` 让报告好看。
- **D7 的两条失败路径形状不同**，别混：
  - 降级链**耗尽** → `_abort(STOP_PROVIDER_UNAVAILABLE)`，yield 一个 **done 帧**，
    HTTP 仍是 **200** + `stop_reason=provider_unavailable`，流式 done 帧再带
    `retriable=true`。D7-H01 钉的就是这条。
  - HTTP **503** → 只有 `ProviderUnavailable` 异常**未被捕获地冒泡**到
    `app/api/errors.py` 的处理器才有。但 `stream_with_retry` 在 `overloaded` 标记下
    会把 `ProviderUnavailable` 转成 `ProviderOverloaded`，而 loop 只捕获后者——所以
    过载导致的耗尽永远走上面那条，落不到 503。
- **Mock 的回声里嵌了 model 名**（`[mock:{request.model}]`），且剥掉用户文本里的
  `[[tool:...]]` 指令、**不回显工具结果**。所以：想断言「工具真的干活了」必须打在
  `tools[].result_contains` 上，写成 `reply_contains` 会是假通过；想断言「降级真的
  换了模型」就打 `[mock:xxx]`（D7-T01 的观测点）。
- **记忆召回只注入 system prompt**，Mock 不回显它、也没有记忆读取 API、
  `mark_used` 也不落库。所以「召回是否发生」只能在「provider 那一轮实际收到的
  system」这个边界上观测（`recalled_prompt_contains`）；「写进去了吗」只能直接查
  `DbMemoryStore`（`memory_user_scope_contains`）。

## bad case 回流

新发现的失败 → 新文件 `evals/cases/{dimension}/{id}.yaml`，`layer` 多为 `hard`，
题注写「来源：某次改动 / 某次面试追问」。第一版不建单独的 `backlog/`，避免两套题本。
