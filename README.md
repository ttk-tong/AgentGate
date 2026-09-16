# AgentGate

> **AI Agent 网关与运行时** —— 把「裸调 LLM API」升级为一个生产级 Agent 底座：
> 会话事件溯源、工具并发治理、上下文自动压缩、Provider 降级熔断、长期记忆与技能激活、子 Agent 并行委派，全部开箱即用。
> ![Python](https://img.shields.io/badge/python-3.11+-blue)
> ![License](https://img.shields.io/badge/license-MIT-green)

## 为什么需要 AgentGate？

| 裸调 API 的问题 | AgentGate 的方案 |
|---|---|
| 对话历史线性堆积，无法回放/分支/审计 | **事件 DAG**：父指针 + 逻辑父指针双链，纯函数投影，支持压缩边界与 sidechain 隔离 |
| 工具并行调用产生副作用竞态 | **读写分批执行器**：只读工具并发、写工具串行，副作用延迟到批末按模型原始顺序应用 |
| 长对话超出上下文窗口就崩 | **分层压缩**：microcompact（占位化旧工具结果，保住 prompt cache）→ 全量摘要 + 熔断防死循环 |
| Provider 过载/截断/超时直接报错 | **韧性链**：指数退避重试 → 熔断器 → 模型降级链 → 截断续写，每条恢复路径带上限 guard |
| 危险操作模型说了就做 | **两段式关卡**：dangerous 工具挂起会话，人工批准/拒绝后恢复 |
| 每会话从零开始，没有记忆 | **长期记忆**：`remember` 工具 + 三级廉价召回（索引扫描 → 关键词粗排 → 小模型精选），无向量库依赖 |
| 提示词一改缓存全失效 | **分层 Prompt 组装**：静态前缀带版本号求缓存 hash，动态块不破坏缓存前缀 |
| 复杂任务只能单线程磨 | **子 Agent fan-out**：`spawn_agent` 并行委派，独立工具集收紧权限，中间过程不污染父上下文 |
| 多 agent 一跑就失控：无限递归、成本不可见、子 agent 绕过 scope | **委派治理四道闸**：深度/扇出/token 预算/fleet 并发，能力只减不增（写进类型），整棵子树用量并进父账，`subagent` 事件实时可见 |

## 架构总览

```mermaid
flowchart TB
    Client["客户端 / React 控制台"] --> GW

    subgraph GW["网关层 api/"]
        AUTH["认证 API Key → Principal<br/>限流 令牌桶 + 并发槽"]
    end

    GW --> LOOP

    subgraph LOOP["Agent Loop（显式状态机 orchestration/）"]
        SM["PRE_CALL → LLM_CALL → TOOL_EXEC → STOP<br/>压缩/降级/续写/确认分支均带 guard"]
        TE["工具执行器：读写分批<br/>只读并发 · 写串行 · 副作用按序延迟应用"]
        SUB["SubagentRunner<br/>spawn_agent 并行 fan-out，sidechain 隔离"]
    end

    LOOP --> CTX

    subgraph CTX["上下文与记忆 context/"]
        DAG["事件 DAG 投影<br/>compact_boundary 截断 · 环检测"]
        CMP["分层压缩<br/>microcompact → 全量摘要 → 熔断"]
        MEM["长期记忆<br/>三级召回 · dedup 写入"]
    end

    LOOP --> RT

    subgraph RT["路由与韧性 routing/ + resilience/"]
        RTR["ModelRouter 能力过滤 + 降级链"]
        RES["退避重试 · 熔断器 · 413 反应式压缩"]
        PROV["Anthropic / OpenAI 兼容 / Mock"]
    end

    RT --> LLM["LLM Provider"]

    subgraph ASYNC["异步通道 async_/"]
        Q["Redis Streams 队列<br/>延迟投递 · DLQ · XAUTOCLAIM"]
        W["Worker 幂等短路 + 退避重试"]
        SCH["Scheduler 分布式锁去重"]
    end

    LOOP -.记忆抽取 / 会话固化.-> Q --> W
    SCH --> Q

    PG[("PostgreSQL<br/>事件 DAG · 租户 · 记忆")] --- LOOP
    RD[("Redis<br/>锁 · 限流 · 队列 · 熔断状态")] --- GW
```

**设计基调**：时钟、随机源、存储全部依赖注入 —— 熔断/退避/限流/认证/队列/记忆召回都是纯逻辑，**离线单测不起 DB/Redis/LLM**；接线层只做装配与 HTTP 映射。

## 快速开始（Docker 一键起）

```bash
git clone https://github.com/ttk-tong/AgentGate.git && cd AgentGate
docker compose up -d --build
```

启动服务列表：

| 服务 | 地址 | 说明 |
|---|---|---|
| **app** | http://localhost:8000 | API（`/docs` Swagger，dev 自动建表） |
| **worker** | — | 异步任务消费 + 定时调度 |
| postgres / redis | 5432 / 16379 | 存储 |
| prometheus | http://localhost:9090 | 指标 |
| grafana | http://localhost:3001 | 仪表盘（admin/admin，已预置 AgentGate dashboard） |

**无需任何 API Key 即可跑通**：不配置 Provider 凭证时自动用 Mock 回声模型。要接真实模型，导出环境变量后重启：

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # Anthropic 原生
# 或 OpenAI 兼容端点（如 DeepSeek）：
export OPENAI_BASE_URL=https://api.deepseek.com/v1 OPENAI_API_KEY=sk-...
docker compose up -d app worker
```

### 30 秒对话冒烟

```bash
# 1. 建会话
SID=$(curl -s -X POST localhost:8000/v1/sessions \
  -H 'Content-Type: application/json' -d '{"external_user":"demo"}' | python -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

# 2. 发消息（非流式；流式加 /stream 走 SSE）
curl -s -X POST localhost:8000/v1/sessions/$SID/messages \
  -H 'Content-Type: application/json' -d '{"content":"你好，介绍一下你自己"}'

# 3. 触发工具调用（Mock 模型支持脚本语法）
curl -s -X POST localhost:8000/v1/sessions/$SID/messages \
  -H 'Content-Type: application/json' -d '{"content":"[[tool:weather city=Beijing]]"}'
```

### 认证与多租户（生产形态：B2B 每客户一套 key）

**隔离模型**：一个企业客户 = 一个 **租户（tenant）**，给它签发一把（或多把可轮转的）API Key。客户后端持有 key，代自己的终端用户调用；终端用户标识放进 `external_user`，仅用于会话归属 / 记忆隔离 / 审计，**不参与鉴权**——租户隔离由 `tenant_id` 硬校验保证，session_id 泄露也调不动别的租户的会话。

> **隔离边界是租户，不是 key，也不是 `external_user`。** 跨租户一律拒绝（硬保证）；但**同一租户内**的多把 key、不同 `external_user` 之间**不互相隔离**——session_id，若泄露给同租户的另一把有 `sessions:*` 权限的 key，是可以访问的。这符合 B2B 场景（key 由客户后端持有，租户内会话本就归该客户统一管理）。

dev 默认 `AUTH_REQUIRED=false` 匿名放行（所有人归一个匿名租户，仅供本地调试）。compose 里 app 服务已默认 `AUTH_REQUIRED=true`，即生产形态；临时敞开用`AUTH_REQUIRED=false docker compose up -d app`。

> **非 dev 环境的启动自检（会让进程起不来，不是 warning）。** `APP_ENV` 不是 `dev` 时，以下两种配置直接抛 `ValueError` 拒绝启动，两个问题一次报全：
>
> - `AUTH_REQUIRED=false` —— 未带凭证的请求会拿到匿名 Principal，等于把接口挂到公网；
> - `AUTH_SALT` 仍是仓库里自带的 `dev-insecure-salt-change-me` —— 算 key 哈希的材料在公开仓库里，哈希等于没做。
>
> 「漏配一个环境变量就把发 key 接口敞开」是这类网关最常见的事故，所以拦截点放在**构造 Settings** 时，而不是靠部署文档提醒。匿名 Principal 的 scope 集合另外硬编码为不含 `admin:*`（第二道防线：即便 `APP_ENV` 被误写成 `dev` 部署到线上，`/v1/admin/*` 仍然拒绝）。两道防线都有测试钉住（`tests/test_config_security.py`）。

**管理接口**（`/v1/admin/*`，全部要求 `admin:*` scope，禁止签发特权 key，杜绝租户自助提权）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/admin/tenants` | 开通企业客户（租户）+ 限流配额 |
| `POST` | `/v1/admin/tenants/{tid}/keys` | 给租户签发 key（明文只返回一次） |
| `GET` | `/v1/admin/tenants/{tid}/keys` | 列 key（不含明文/哈希，供轮转决策） |
| `DELETE` | `/v1/admin/tenants/{tid}/keys/{kid}` | 吊销 key（软删除，认证层即时拒绝） |

```bash
# 0. 引导一把平台 admin key（一次性；只落哈希，明文只打印这一次）
docker compose exec app python -m scripts.seed_api_key \
  --tenant-name platform-admin --scopes "admin:*"
ADMIN="ak_..."   # 保存

# 1. 开通企业客户（租户）+ 配额
TID=$(curl -s -X POST localhost:8000/v1/admin/tenants \
  -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"name":"Acme","qps":10,"burst":20}' | tr -d '{}"' | sed 's/.*tenant_id://;s/,.*//')

# 2. 给它签发一把业务 key（scope 限 sessions:*，发不了 admin:*）
curl -s -X POST localhost:8000/v1/admin/tenants/$TID/keys \
  -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"scopes":["sessions:write"],"name":"acme-prod"}'
# → {"api_key":"ak_...", ...}  明文只此一次，交给客户

# 3. 客户用这把 key 调用；终端用户标识放 external_user
curl -s -X POST localhost:8000/v1/sessions \
  -H "Authorization: Bearer <客户key>" -H 'Content-Type: application/json' \
  -d '{"external_user":"acme-user-42"}'

# 4. 轮转：发新 key → 客户切换 → 吊销旧 key（下一次请求即 401）
curl -s -X DELETE localhost:8000/v1/admin/tenants/$TID/keys/<旧KID> \
  -H "Authorization: Bearer $ADMIN"
```

密钥安全设计：`ak_<prefix>_<secret>` 格式，服务端只存 `sha256(salt+secret)` 哈希 + 明文prefix；验证走常量时间比对；跨租户访问硬校验拒绝；吊销/过期即时生效。超限返回`429 + Retry-After`（令牌桶精确计算补足时间）。

### 前端控制台（可选）

```bash
cd web && npm install && npm run dev   # http://localhost:3000
```

### 本地开发（不走 Docker）

```bash
docker compose up -d postgres redis
python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
cp .env.example .env
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m pytest -q                                        # 全部离线可跑
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

## 性能与可观测

单机压测（Mock provider，详见 [`ops/benchmark.md`](ops/benchmark.md)，含缺陷定位与修复过程）：

- 流式对话首 Token（TTFB）**P50 ~25ms**
- 创建会话 50 并发：~30 req/s，P95 1.56s，**零错误**
- 限流精确性：超 `max_concurrency` 精确 429 + Retry-After，无误放行
- 压测暴露并修复两个真实缺陷：Redis 连接无超时阻塞主链路（4100ms → 100ms）、DB 连接池默认过小

可观测三件套：structlog 结构化日志（trace_id 贯穿）+ Prometheus 指标（HTTP/Agent/工具/Provider/队列五组，路由模板做 label 防高基数爆炸）+ Grafana 预置仪表盘与告警规则。

## 测试

19 个测试文件、约 2800 行。核心策略：**纯逻辑离线测**（投影/分批/压缩规划/退避/熔断/限流/认证/队列/记忆/技能/Prompt 组装，不起任何外部依赖）+ **关键链路端到端**（对话、工具确认、压缩、恢复路径，起 PG/Redis）。CI 跑 ruff + mypy + Alembic 迁移校验 + pytest（带覆盖率报告）。

```bash
pytest -q --cov=app --cov-report=term-missing
```

## 运行时评测集（`evals/`）

测试回答「代码坏没坏」，评测回答「**能力面强弱在哪、改一处之后是变好还是变差**」。
32 道端到端任务题（一条输入 → 一次完整 run），provider 固定 Mock，断言全部是规则式的，
**不测答案质量**，只测编排、分批、落库、取消/引导/引用、闸门与优雅失败是否按契约发生。
边界、题本约定与已知坑见 [`evals/README.md`](evals/README.md)，报告见
[`evals/reports/latest.md`](evals/reports/latest.md)。

```bash
.venv/Scripts/python.exe -m evals.runner --all --report   # 全集 + 出报告，约 60s
.venv/Scripts/python.exe -m evals.ablation                # A–D 四列消融，约 4 分钟
```

**实测（commit `95c52ae`，Mock + PG + Redis，全集 ~60s）**：

| 能力面 | 通过 | 说明 |
|---|---|---|
| D1 工具编排 | 8/8 | 并发批/串行批、参数、步数上界、无孤儿 `tool_use` |
| D2 上下文压缩 | **2/3** | 红的那道钉的是真缺口，见下 |
| D3 长期记忆 | 3/3 | 写入落库 + 召回注入 system 两个边界分别断言 |
| D4 对话状态 | 8/8 | 取消/引导/引用/double-texting 抢占 |
| D5 子 Agent 治理 | 5/5 | 深度、扇出、预算、fleet 并发四道闸 |
| D6 安全与鉴权 | 3/3 | 跨租户、沙箱越权、注入不提权 |
| D7 韧性 | 2/2 | 降级链换模型成功 / 链耗尽优雅失败 |
| **合计** | **31/32 (97%)** | 分层 8 基础 / 14 绕弯 / 10 刁难 |

**故意没有「总分」**：32 题的算术平均会把「哪一面弱」摊平。刁难层不是 100% 是设计允许
的结果——那一层收的就是取消中途、越权、注入这类优雅失败题，其中一部分钉的是**已知产品
缺口**，题注里写明根因与反证方式。

唯一的红：**D2-H01**（压缩连续失败到熔断）。根因是熔断路径上异步生成器的 `finally`
（清 `session.active_compaction`）不在 `yield ..., True` 之后同步执行，要等 GC 回收生成器
才跑，那时请求作用域的 DB session 已关闭 → 标记留在 `"auto_compact"` → **收尾轮挂住**。
修产品是单独一条 PR 的事；在那之前这题以红色形式记录缺口，**不改松 `expect` 让报告好看**。

消融对比（[`evals/reports/ablation.md`](evals/reports/ablation.md)）验的不是「哪列分高」——
B/C/D 都是刻意把产品调偏，分低是预期。看的是**变化有没有落在该变的维度上**：

| 方案 | 旋钮 | 合计 | 翻面落点 |
|---|---|---|---|
| A 基线 | 默认 settings | 31/32 | — |
| B 关子 Agent | `SUBAGENT_ENABLED=false` | 25/32 | **只崩 D5**（5/5 → 0/5，错误是明确的 `permission_denied`），D1 零影响 |
| C 压缩更激进 | 阈值压到 400 token | 31/32 | **零翻面**——压缩触发更早但不改变任何一题的契约结果 |
| D 双发 reject | 策略换 `reject` | 29/32 | **只崩 D4-T04**（200 抢占 → 409） |

隔离度是这张表真正的结论：关子 Agent 没把工具编排带崩，换双发策略没影响压缩。
（B、D 两列另有 D4-T01 翻面，但那是 `repeats: 5` 的时序题实测 4/5 的**竞态抖动**，不是旋钮
效应，表里标了 `n/5`。）

评测集**不进 CI 必过路径**，退出码恒为 0——「完成率 97%」是合法结果，不是 CI 红。

## 目录结构

```
app/
├── api/            # 路由 + 认证/限流/trace 中间件
├── orchestration/  # Agent Loop 状态机、工具执行器、子 Agent、Prompt 组装、技能
├── context/        # 事件 DAG 投影、分层压缩、tokenizer、长期记忆
├── routing/        # ModelRouter + Provider 适配器（Anthropic / OpenAI 兼容 / Mock）
├── resilience/     # 退避、熔断、限流（纯逻辑，时钟/随机注入）
├── security/       # API Key 生成/验证、Principal、scope 鉴权
├── async_/         # 队列（Redis Streams）、Worker、Scheduler、分布式锁
├── persistence/    # SQLAlchemy 异步引擎、ORM 表、Redis 客户端
├── domain/         # 跨层领域模型（事件/工具/记忆/技能/子Agent）
└── observability/  # structlog + trace + Prometheus 指标
web/                # React 控制台（Vite + Tailwind）
alembic/            # 数据库迁移
ops/                # Prometheus/Grafana 配置 + 压测报告
scripts/            # API Key 签发等运维脚本
tests/              # 离线单测 + 端到端
evals/              # 运行时评测集（题本 + harness + 报告，不属于运行时）
```

## 设计细节（分阶段实现记录）

<details>
<summary><b>阶段 1：事件 DAG + 最小 Loop</b></summary>

不带工具的对话端到端闭环（walking skeleton）：

- **DAG 投影**（`context/projection.py`）：父指针回溯 + `compact_boundary` 截断 + 按 `message_id` 归并并行兄弟节点 + 环检测 + sidechain 排除。纯函数，单测覆盖。
- **协议不变式两道防线**：每条带 `tool_calls` 的 assistant 消息，在下一条 assistant 之前必须有配对的 `tool_result`。中断（确认超时 / 进程崩溃 / 各类中止分支）会留下没有结果的 `tool_use`——而 DAG 是 append-only，**删不掉**，非法序列会每一轮被重新投影出去、被 provider 每一轮 400，会话永久报废。所以：事件层在每轮入口自愈（`heal_orphan_tool_calls` 幂等补写真实配对事件），投影层再兜一次底（`_close_orphan_tool_calls` 合成结果）。
- **Provider 适配器**（`routing/providers/`）：Anthropic Messages / OpenAI 兼容两套 SSE 解析；无 API key 时用 Mock，保证无网络也能跑通。**跨 provider 语义等价**是硬约束——上层 Loop 只认 `StreamChunk` 和 `PromptTooLong / ProviderOverloaded / ProviderUnavailable`，任何一个适配器漏掉状态码→异常的映射，反应式压缩、重试、模型降级、熔断器就会在那条路径上**全部静默失效**。所以状态判定只存在一份（`providers/http_errors.py`，含 200 之后的流中 `error` 事件），并由 `tests/test_provider_contract.py` 用**同一批断言**分别打到两种线上格式（36 个用例）。
- **最小 Agent Loop**（`orchestration/agent_loop.py`）：显式状态机 `PRE_CALL → LLM_CALL → STOP → DONE`，命名转移与恢复 guard 字段一步到位。
- **会话串行锁**（`orchestration/session_lock.py`）：`lock:session:{id}`，Redis SET NX + Lua 校验释放。

</details>

<details>
<summary><b>阶段 2：工具（读写分批 + 人工确认）</b></summary>

- **工具契约**（`domain/tool.py`）：`ToolSpec`（`is_read_only` / `is_concurrency_safe` / `mutates_context` / `dangerous`）+ 三段式执行体（模型面校验 / 系统面权限 / 执行）+ `ContextMutation`。
- **读写分批执行器**（`orchestration/tool_executor.py`）：连续只读工具并成「可并发批」、写/未知/不安全工具单独串行批；并发批的 `ContextMutation` **延迟到批结束后按模型原始调用顺序串行应用**，避免竞态。
- **两段式关卡**：`dangerous` 工具挂起会话为 `waiting_confirmation`，`POST /v1/sessions/{id}/confirmations` 批准/拒绝后恢复运行。

</details>

<details>
<summary><b>阶段 3：上下文管理（分层压缩）</b></summary>

- **Token 计量 + 预算**：轻量启发式估算（中英分别校准，宁大勿小）；`compact_threshold = 有效窗口 - BUFFER`。
- **microcompact**（日常主力）：白名单只读工具的旧结果占位化、token 归零。语义可逆、不改父指针、不重排消息 → **保住 prompt cache**。
- **全量摘要 + `compact_boundary`**：九段式结构化摘要；边界事件 `parent_id=None` 切断前史、`logical_parent_id` 保留真实指向供回放审计。
- **失败熔断**：摘要失败累计达 `max_compact_failures` 熔断退出，不死循环；413 反应式压缩带一次性 guard。

</details>

<details>
<summary><b>阶段 4：韧性（恢复路径 + 认证限流）</b></summary>

- **认证/鉴权**（`security/`）：API Key 哈希存储 → `Principal`；scope 校验 + 租户隔离硬校验。
- **多租户管理**（`api/v1/admin.py`）：`admin:*` 保护的 `/v1/admin/tenants[/{tid}/keys]` 发/列/吊 key，白名单禁签 `admin:*`（防租户自助提权）；吊销软删除即时生效。B2B 每客户一租户，`external_user` 承载客户内部终端用户标识（不参与鉴权）。
- **重试/熔断/降级链**（`resilience/`）：指数退避 + 抖动 + Retry-After；每 Provider 熔断器（CLOSED→OPEN→HALF_OPEN）；降级链耗尽映射 503。
- **Loop 恢复路径**：过载 → 模型降级重跑；`max_tokens` 截断 → 升限续写；**错误抑制**——首字节前失败可安全重跑，已产出 token 后以 error 帧收尾不吞流。每条路径带上限 guard。
- **限流**：租户 QPS 令牌桶（Redis Lua 原子）+ 并发槽位（异常路径也释放，TTL 兜底）。

</details>

<details>
<summary><b>阶段 5：异步能力（队列 + Worker + 调度）</b></summary>

- **队列抽象**（`async_/queue.py`）：`InMemoryQueue`（离线测试）与 `RedisStreamsQueue`（消费者组 + `XAUTOCLAIM` 回收崩溃 Worker 的消息；延迟消息走 ZSet 到点搬运）。
- **Worker**：幂等短路（`idempotency_key` + `DoneStore`）→ 执行 → 可重试错误退避重排 → 超限进 **DLQ**。
- **Scheduler**：`fire_job` 抢分布式锁（SET NX PX）才入队，多实例同一 cron 只触发一次。

</details>

<details>
<summary><b>阶段 6：记忆 + 技能 + 提示词分层</b></summary>

- **记忆基线**：**不上向量库**。三级廉价召回：索引头部扫描 → 关键词 + 重要度粗排（零 LLM 成本）→ 候选过多才用小模型精选 top-k。写入按 `dedup_key` 去重。scope 三级隔离防跨用户泄漏。
- **提示词分层组装**：静态前缀（带版本号）在前、动态块在后；对 cacheable 块求 `sha256` 缓存前缀 hash —— 静态不变则命中 Provider prompt cache。外部内容统一 `<memory>` 边界包裹声明「仅为数据」，纵深防注入。`debug()` 支持 dry-run 观测。
- **技能加载**：扫描 `SKILL.md`（极简 front-matter，不引 YAML 依赖）；两级激活（trigger 关键词 → 小模型裁决）+ scope 过滤 + `MAX_ACTIVE` 上限。

</details>

<details>
<summary><b>阶段 7：子 Agent（隔离 + fan-out）</b></summary>

- **隔离执行体**：`SubagentRunner` 跑受限完整子 Loop——独立工具集（`allowed_tools` **替换而非合并**父工具集）、独立事件流（不落父 DAG），只回传最终文本。
- **并行 fan-out**：`spawn_agent` 标记 `is_read_only + is_concurrency_safe` → 执行器自动把多个调用归入同一并发批并行。
- **sidechain 语义**：子过程标记事件不改父 head，投影自动跳过——中间过程不污染父 LLM 上下文，但保留审计。

</details>

<details>
<summary><b>阶段 8：MCP 外部工具接入（三层信任映射）</b></summary>

- **两种传输**（`mcp/transport/`）：stdio（本地子进程，`npx` / `uvx` 冷启动给到 90s 握手超时）与 Streamable HTTP。Manager 是**进程级常驻**的，握手成本不摊到任何一次对话延迟上。
- **三层信任映射**（`mcp/mapping.py`，整个集成的判断核心）：MCP server 是第三方代码，其 annotations 按规范只是 hint。所以 —— 第 1 层运维配置 `readonly_tools`（**唯一**能授予并发安全的途径，因为并发跑写操作的后果由部署方承担）；第 2 层 server annotations（只用于**收紧**，如 `destructiveHint` → 强制人工确认，以及填充非安全关键字段）；第 3 层保守默认（写工具、串行）。一句话：**annotations 可以让工具更受限，不能让工具更自由。**
- **命名空间**：`{server}__{tool}`，用 `__` 而非 `:` / `.`（function name 合法字符集是 `[a-zA-Z0-9_-]`），超 64 字符截断且保持唯一。
- **scope 默认拒绝**：代理工具声明 `mcp:{server}`，必须拿到匹配 scope（或 `mcp:*`）才放行。没有请求主体的内部路径要用 `ToolContext.internal=True` 显式声明。
- **单条配置坏掉不阻塞启动**：解析失败的 server 条目告警跳过，其余照常加载；含凭证的 `headers`/`env` 值永不进日志（`redacted()`）。

> ⚠️ **相对早期版本的行为变更**：`granted_scopes` 为空时，MCP 代理工具从「放行」改成「拒绝」。旧语义下任何漏传 scope 的调用路径（子 agent 就是一例）都会**静默**变成完全授权——这类"失败开放"的默认值是安全审计里最典型的一条。升级后如果 MCP 工具突然被拒，是缺 scope，不是 bug：给 key 加上 `mcp:{server}`，或给内部调用路径设 `internal=True`。

</details>

<details>
<summary><b>阶段 8b：多 Agent 治理（栈深保护 + 成本可见）</b></summary>

设计与分期计划见 [`.claude/plan/12-multi-agent.md`](.claude/plan/12-multi-agent.md)。核心定位：**子 agent 是「LLM 层面的函数调用」，属于上下文管理，不是编排框架**——压缩是垃圾回收（事后清理已进主堆的垃圾），子 agent 是栈帧回收（垃圾从不进主堆）。

本阶段（M1）先把阶段 7 的地基做对。四个缺陷由离线探针实测确认，不是代码审查推断：

| 缺陷 | 实测证据 | 修法 |
|---|---|---|
| 递归无上限 | 子 agent 能看到 `spawn_agent`，探针跑出 **6 层嵌套** | `FleetGovernor` 深度闸，深度随 `ToolContext.agent_depth` 逐层递增（旧版存在 runner 上，全树共用一个实例，所以孙 agent 也报 depth=1） |
| 子 agent 绕过 MCP scope 校验 | 子 `ToolContext` 实测为 `tenant_id='' trace_id='' granted_scopes=[]`，而 MCP 代理把空 scope 当「未注入 → 不设卡」放行 | `AgentRunContext.child()` 是**唯一**派生途径且强制与父求交——能力放大在结构上不可能 |
| 成本完全不可见 | 子 agent 实测烧 1234 进 / 567 出，父 `usage` 报告 **0** | 每轮扣减全树共享的 `TokenBudget`；整棵子树用量随返回值并进父账 |
| fan-out 并发写同一 `AsyncSession` | 旧版子 agent 自己 `append_event`，N 个并发子 agent 共用请求作用域的同一个 session | 审计 trace 改走 `ContextMutation`，由父 loop 在批末**按模型原始调用顺序**串行落库 |

- **四道闸**（深度 / 扇出 / 预算 / fleet 并发）：与「每条恢复路径都带 guard」是同一条铁律的树版本。判定放在 `check_permissions`——`deny` 会被执行器折成 error 结果回填，模型看得懂并会改策略。
- **流式可见**：`subagent` 事件（started/finished/failed/denied）在**工具批执行期间**就流出去，不用等一次 300 秒的 fan-out 跑完；配 5 个 Prometheus 指标回答观测四问「扇出几个 / 烧了多少 / 跑了多久 / 多深」。
- **韧性对等**：抽出 `llm_call` 让父子共用退避重试 + 熔断；子上下文超限走内存版 microcompact（占位化最旧工具结果，一次性 guard）。
- **dangerous 工具在子 agent 侧直接从工具集滤掉**：子 loop 状态只在内存，挂起-确认无从恢复。
- ⚠️ **行为收紧**：`SUBAGENT_MAX_DEPTH` 默认 1，嵌套派发会被明确拒绝（阶段 7 是无限）。放宽应等 M2 的 `delegates_to` 清单白名单落地。

后续：M2 清单化 agent（`AGENT.md` + 结构化输出）、M3 拓扑（接力 / 汇总 / 显式编排 API）。

</details>

<details>
<summary><b>对话状态追踪：打断 / 引导 / 引用 / double-texting</b></summary>

设计见 [`docs/superpowers/specs/2026-09-10-conversation-state-tracking-design.md`](docs/superpowers/specs/2026-09-10-conversation-state-tracking-design.md)。控制面全走 Redis（多 worker 下 cancel/steer 请求可能落在任意实例）。

| 端点 | 说明 |
|---|---|
| `POST /v1/sessions/{id}/runs/{run_id}/cancel` | 取消指定运行（202，协作式） |
| `POST /v1/sessions/{id}/cancel` | 取消该会话当前运行（202） |
| `POST /v1/sessions/{id}/runs/{run_id}/steer` | 向进行中的运行追加引导消息（202） |

消息请求（`POST /v1/sessions/{id}/messages[/stream]`）新增可选字段：

- `references`: `[{"ref_type": "file|message|memory|kb", "ref_uri": "...", "render_mode": "inline|summary"}]`
  最多 20 条。解析失败返回 404/403/422，不会写入会话（解析在锁外、落库在锁内，失败零副作用）。

创建会话（`POST /v1/sessions`）新增可选字段：

- `concurrency_policy`: `interrupt`（默认）| `reject`。`enqueue` / `rollback` 暂返回 501（不静默降级）。

取消是**协作式**的：框架保证「不会进入下一个检查点」，不保证「立刻停下正在做的事」。已发出的 provider 请求与已启动的工具调用会跑完当前步骤。`done` 帧的 `stop_reason` 据此区分 `cancelled_by_user`（用户按停止）与 `superseded`（被新消息顶掉）。

</details>

## License

[MIT](LICENSE)
