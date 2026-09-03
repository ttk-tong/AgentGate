# AgentGate 加固设计（2026-09-02）

## 1. 背景与定位

AgentGate 已按 8 个阶段建成：事件 DAG + 投影、工具读写分批、分层压缩、韧性链、
异步队列、记忆/技能、子 Agent、MCP 接入。功能面是完整的，但这一轮的目标不是加功能。

**定位：开源作品集 / 求职名片，要经得住逐行审代码。**

这个定位决定了取舍。一个 reviewer 打开仓库，最先做的两件事是跑 CI 和拿 README 的
说法去对代码。所以本轮的判据只有一条：

> **README 里的每一条说法，都要有代码支撑 + 有测试守着。**
> 没有支撑的说法，要么补实现，要么删掉/降级为 Roadmap —— 不留「听起来很强但是假的」。

主链路口径也一并钉死：**主 provider 路径是 OpenAI 兼容端点**（DeepSeek 等代理）。
Anthropic 原生路径是第二条路，它的可用性是 P1，不是 P0 —— 但「语义等价」是硬约束，
因为上层的降级/重试/压缩全都建立在两条路抛同一套异常的前提上。

## 2. PR 划分（小步、可独立审）

| PR | 主题 | 状态 |
| --- | --- | --- |
| PR1 | 修复优先：跨 provider 契约、协议不变式、启动安全、CI 门禁 | **已完成** |
| PR2 | 承诺对齐：异步通道接线、召回第三级、README/Roadmap 校准、死 schema 清理 | 待批准 |
| PR3 | 能力补齐：会话历史/列表/取消端点、token 与成本计量、连接池复用、`LoopConfig` 注入 | 待批准 |

顺序是 TDD 的：先让「已经写了但没测试守着」的代码有测试，再动新功能。因为这批代码
的失效模式几乎全是**静默的**——不报错，只是功能不再生效。没有测试的话，下一次重构
会把它们一个一个悄悄改回去。

## 3. PR1 · 修复清单

### 3.1 跨 provider 契约（本轮最大的一处真实缺陷）

Anthropic 适配器此前有四处**各自独立的静默失效**：

1. **payload 里根本没有 `tools` 字段** → 整个工具子系统在这条路径上是死的。模型永远
   不会发起调用，而且一声不响，看起来就像「模型不爱用工具」。
2. **`Role.tool` 消息被角色过滤器整批丢掉**（旧代码只保留 `user`/`assistant`）→ 模型
   看不到任何工具结果，同时请求本身还是协议非法的。
3. **不解析 `tool_use` / `input_json_delta`** → 即便模型调了工具也收不到。
4. **裸 `raise_for_status()`** → 不抛 `PromptTooLong` / `ProviderOverloaded`，于是反应式
   压缩、重试、模型降级、熔断器**在这条路径上全部失效**。

外加一处会打死所有当前模型的问题：**无条件发送 `temperature`**。新一代模型
（Opus 5 / Sonnet 5 / Fable 5 等）已移除该参数，收到即 400。改成 `None` 表示不发，
只有调用方显式设了才发。

**修法**：状态码 → 领域异常的判定抽到 `routing/providers/http_errors.py`，两个适配器
共用**同一份**。同时补上一个两边都缺的场景：握手 200 之后 provider 仍可能在 SSE 流里
发 `{"type":"error"}`（过载最常见）。不把它归一成领域异常的话，上层只看到一个提前
结束的空流——既不重试也不降级，对用户就是「模型什么都没说」。

**守护方式**：`tests/test_provider_contract.py` 把同一批行为断言分别打到两种线上格式
（各自的 dialect 类负责翻译 wire 格式），13 个测试 → 36 个参数化用例。用
`httpx.MockTransport` 注入，不碰网络。覆盖：文本流 / usage / finish、system 上线、
工具声明上线、空工具集不发 `tools` 字段、分片工具入参拼接、工具结果回写请求、
413 与 400-context-overflow → `PromptTooLong`、400 其它 → 原样抛、
429/500/502/503/529 → `ProviderOverloaded`、401/403 → `ProviderUnavailable`、
截断 → `max_tokens`、未设 temperature 不发。

顺带把 `AsyncClient` 改成可注入：测试挂 MockTransport，生产可复用连接池（复用本身
留到 PR3）。注入的 client 生命周期归调用方，适配器不 close 它。

### 3.2 协议不变式：孤儿 `tool_use`

**为什么这是最狠的一类 bug**：投影是纯函数，每一轮都从 append-only 的 DAG 重建。
一条非法消息形状不是「这次失败」，而是**每一次都失败、而且删不掉**——会话永久报废。

产生孤儿的路径全是异常路径：确认超时、进程崩溃，以及 Loop 自己的三条中止分支
（assistant 事件在「要不要执行工具」之前就落库了）。本轮按用户确认的方案做**两层**：

- **事件层自愈**：`heal_orphan_tool_calls` 在每轮 `run()` 入口跑，往 DAG 真正补写配对
  事件。必须幂等——不然每轮多一条垃圾事件。
- **投影层兜底**：`_close_orphan_tool_calls` 每轮合成结果。防的是「自愈还没跑到 / 新增
  了一条没想到的中止路径」。

三条中止分支各带精确 reason，原样落库供排查：`output_truncated`（截断时工具入参
大概率也是残缺的，一律不执行）、`finish_reason_mismatch`（少数端点给了 tool_calls 却
报 stop）、`max_tool_calls`。

**守护方式**：`tests/test_projection.py`（+5，含「正常轮次不被兜底逻辑改写」的反向
断言）、`tests/test_loop_dag_invariants.py`（+7，内存假 store，含幂等性与 clean 会话
零改动）、`tests/test_loop_recovery.py`（+3，DB 落库，三条中止分支各钉一条，统一用
`find_orphan_tool_calls(...) == []` 断言不留孤儿）。

### 3.3 启动安全自检

`_validate_prod_security` 是「漏配一个环境变量就把发 key 接口挂到公网」这类事故的唯一
拦截点。它只在构造 `Settings` 时跑一次，出错就该让进程起不来。这种行为一旦被后来的
重构悄悄改成 warning，只有测试能发现。

非 dev 环境下两种配置直接拒绝启动，且**两个问题一次报全**（别让部署方修一个重启
一次）：`AUTH_REQUIRED=false`、`AUTH_SALT` 仍是仓库自带值。dev 必须仍能零配置起来，
否则本地调试成本被这道校验毁掉——这一条也有测试。

第二道防线：匿名 Principal 的 scope 集合硬编码为不含 `admin:*`。即便 `APP_ENV` 被误写
成 `dev` 部署到线上，`/v1/admin/*` 仍然拒绝。两道都测（`tests/test_config_security.py`，
10 个用例）。

### 3.4 CI 门禁真的绿了

对作品集仓库来说，红色的 CI 徽章是伤害最大的一件事。本轮发现两处 CI 实际是红的：

- **ruff**：7 处 F401 未使用导入（`ruff check` 默认规则集含 F）。已清理。
- **mypy 硬门禁**（`mypy app/domain app/config.py`，CI 里不带 `|| true`）：2 处错误。
  - `EventKind.title` 成员名遮蔽 `str.title` 方法。运行时安全，但不改名——这个字面量
    已经落进 `session_event.kind` 列的历史数据。加定向 `type: ignore` + 注释说明。
  - `Tool.call` 的 `on_progress` 缺注解。补 `ProgressFn` 别名，并在 docstring 里写清
    它**目前没有任何调用方**（签名先立在契约里，等长任务工具真要透传进度时再接）。

**顺带修掉一个 Windows 上的可移植性 bug**：`mypy.ini` 由 configparser 按**系统 locale**
解码，里面的中文注释在 GBK 环境（中文 Windows）下直接把 mypy 打崩成
`UnicodeDecodeError`。CI 跑在 UTF-8 的 ubuntu 上所以看不出来，但任何在中文 Windows 上
克隆仓库的人都用不了类型检查。配置迁到 `pyproject.toml` 的 `[tool.mypy]`——TOML 规范
强制 UTF-8，注释想写中文就写中文。`mypy.ini` 删除。

### 3.5 陈旧的默认模型

`default_model` 的默认值刷新，并在三处（`config.py` / `.env.example` /
`docker-compose.yml`）都补上同一句注释：**这里填的是当前 provider 的模型 id**。走
Anthropic 填 `claude-*`，走 OpenAI 兼容端点必须覆盖成该端点的 id（如 `deepseek-chat`），
否则请求会被对端以「未知模型」拒掉。这是接 DeepSeek 代理时最容易踩的一脚。

## 4. Roadmap（本轮明确降级的承诺）

按用户确认的口径：**核心补实现，边缘降为 Roadmap**。以下几条属于「README 说了但没
做」，本轮不实现，改为显式记为 Roadmap，避免继续当成既有能力宣传：

- **prompt cache 断点**（`cache_control` 显式标记）。目前只做到「静态前缀在前 + 求
  hash」，能否命中取决于 provider 的隐式缓存，不是我们主动打的断点。
- **跨 provider 降级**。现有降级链是**同一 provider 内换模型**，不是换 provider。
- **`TaskRow` / `ScheduleRow` 死 schema**：建了表、没有任何读写路径。PR2 里删掉。
- **`_MODEL_WINDOWS` 上下文窗口标定**。

关于最后一条要说清楚：早先我判断 `_MODEL_WINDOWS` 里 Claude 的 200k 是错的（真实值
更大）。本轮**没有改这些数字**。原因是我无法确认更大的窗口是默认值还是需要显式
opt-in 的变体，而两个方向的错误代价不对称：

- 低估 → 压缩提前触发，多花一次摘要，功能正常；
- 高估 → 预算检查放行一个必然 413 的请求，只能靠反应式压缩兜底。

所以保留保守值，把不确定性写进代码注释，把真正的修法（启动时拉一次 provider 的模型
元数据接口并缓存）记进 Roadmap。硬编码一个我没验证过的更大数字，比现状更糟。

## 5. 待批准：PR2 / PR3

**PR2 · 承诺对齐**
- 异步通道接线：队列/Worker 已实现且有测试，但没有任何 API 入口把任务投进去。
- 记忆召回第三级（候选过多 → 小模型精选 top-k）。README 写了三级，代码只有两级。
- 删除 `TaskRow` / `ScheduleRow`（含迁移）。
- README/Roadmap 全量校准：把上一节几条挪进 Roadmap 段落。

**PR3 · 能力补齐**
- 会话历史 / 列表 / 取消端点（现在只能发消息，拿不回历史）。
- token 与成本计量（usage 已经在流里，没有落库聚合）。
- httpx 连接池复用（3.1 已经把注入口开好了）。
- `LoopConfig` 从 `Settings` 注入，而不是各处默认值。

## 6. 遗留与未验证事项

- **`mypy app` 全量扫描仍有 55 处错误**（CI 里是 `|| true` 的告警门禁）。本轮只清了硬
  门禁范围（`app/domain` + `app/config.py`）。逐步收紧，不在本轮。
- **代码注释里引用的 `plan/01`、`plan/04` 等文件不在仓库里**。对 reviewer 是悬空引用，
  应统一改指向本 docs 目录或删除。记为后续清理项。
- **根目录 `test.py`** 是一份并发执行的手写草稿，未纳入本次提交（保持 untracked）。
- 本轮所有验证都在本地跑通：`ruff`（默认规则集）通过、`mypy` 硬门禁通过、
  `alembic upgrade head && alembic check` 无待生成迁移、`pytest` **291 passed**。
