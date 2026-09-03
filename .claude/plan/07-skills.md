# 技能（Skills）

## 1. 目标

技能是"一组能力的可复用打包"：把特定领域的提示词片段、工具集、示例、约束封装成一个可被发现、按需加载、动态激活的单元。它让 Agent 从"通用助手"变成"能在特定场景专业发挥"，且不必把所有能力一次性塞进上下文。

## 2. 技能 vs 工具 vs Agent

| 概念 | 粒度 | 是什么 |
|------|------|--------|
| 工具（Tool） | 最小 | 一个可调用的函数（见 04） |
| 技能（Skill） | 中 | 提示词片段 + 一组工具 + 使用说明 + 示例的打包 |
| Agent | 大 | 身份 + 默认技能集 + 配置 + 路由策略 |

技能是"工具的编排 + 知识"，Agent 是"技能的宿主"。

## 3. 技能定义

采用"声明式清单 + 资源目录"的形式（与 Claude Code 的 skill 目录风格一致，便于用户手写与分发）：

```
skills/
└── invoice_processing/
    ├── SKILL.md          # 清单：元数据 + 使用说明（front-matter）
    ├── prompt.md         # 注入 system 的领域提示词片段
    ├── examples/         # few-shot 示例（可选）
    └── resources/        # 参考资料（可选，按需注入或供工具读取）
```

`SKILL.md` front-matter：

```yaml
---
name: invoice_processing
version: 1.2.0
description: 从发票文档中抽取字段、校验并录入。用于"处理发票/报销单"类请求。
triggers:                      # 用于自动发现（见第 5 节）
  - 发票
  - 报销
  - invoice
tools:                         # 该技能启用的工具（引用注册表中的名字）
  - kb_search
  - sql_query
  - http_request
requires_scopes:               # 激活该技能所需权限
  - finance:read
model_hint: default            # 能力/成本提示，供路由参考（见 01）
max_context_tokens: 2000       # 该技能提示片段的预算上限
---
```

领域模型：

```python
# domain/skill.py
from pydantic import BaseModel

class Skill(BaseModel):
    name: str
    version: str
    description: str
    triggers: list[str] = []
    tools: list[str] = []
    requires_scopes: list[str] = []
    model_hint: str | None = None
    max_context_tokens: int = 2000
    prompt_path: str
    examples_paths: list[str] = []
```

## 4. 加载与注册

启动时扫描 `skills/` 目录，解析 `SKILL.md`，构建 `SkillRegistry`。校验：引用的工具必须在 `ToolRegistry` 存在，否则拒绝加载并告警。

```python
# orchestration/skills/registry.py
class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def load_dir(self, root: str, tool_registry: ToolRegistry):
        for manifest in discover(root):          # 找到所有 SKILL.md
            skill = parse_skill(manifest)
            missing = [t for t in skill.tools if not tool_registry.get(t)]
            if missing:
                raise ValueError(f"{skill.name} 引用了不存在的工具: {missing}")
            self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)
```

## 5. 发现与激活（Skill Selection）

一个 Agent 可能挂载很多技能，但每轮不该全部激活。激活策略分两级：

### 5.1 静态激活
Agent 配置中标记为 `always_on` 的技能始终激活（如通用礼仪、安全约束）。

### 5.2 动态激活
根据用户输入 + 会话状态选择相关技能：

```python
async def select_skills(agent_cfg, user_input, ctx) -> list[Skill]:
    active = [s for s in agent_cfg.skills if s.always_on]
    candidates = [s for s in agent_cfg.skills if not s.always_on]

    # 一级：trigger 关键词 / 结构化匹配（廉价，先过一遍）
    hit = [s for s in candidates if match_triggers(s, user_input)]

    # 二级：命中过多或无命中时，用轻量分类器/LLM 路由裁决
    if len(hit) > MAX_ACTIVE or (not hit and needs_specialization(user_input)):
        hit = await llm_route_skills(candidates, user_input, top_k=MAX_ACTIVE)

    # 权限过滤：无 scope 的技能不激活
    active += [s for s in hit if has_scopes(ctx, s.requires_scopes)]
    return dedup(active)[:MAX_ACTIVE]
```

激活后果（喂给编排层）：

1. 技能的 `prompt.md` 片段进入 system prompt 组装（见 `08`）。
2. 技能的 `tools` 并入本轮暴露给 LLM 的工具集（见 `04`）。
3. `model_hint` 参与路由决策（见 `01`）。
4. 受 `max_context_tokens` 约束，超预算时截断示例而非说明。

## 6. 技能间协作与 Handoff

复杂任务可能需要多技能接力。通过 `handoff` 工具（见 `04`）实现：当前技能判断"这该交给 X 技能/子 Agent"，产出 handoff 调用，编排层切换激活技能集并把必要上下文传递过去。避免所有技能同时激活导致的提示词膨胀与相互干扰。

## 7. 与记忆的关系

- **过程记忆 → 技能**：`06` 中反复出现的成功操作模式可沉淀为候选技能（人工审核后固化），这是技能的"自动生长"路径。
- 技能执行中产生的稳定事实回写语义记忆。

## 8. 版本化与灰度

- 技能带 `version`，会话创建时快照当前激活技能版本，保证一次会话内行为稳定。
- 新版本技能可按租户灰度：`tenant_skill_bindings` 表（见 `10`）控制哪个租户用哪个版本。

## 9. 相关文档

- 提示片段如何拼接：`08-prompt-assembly.md`
- 工具集来源：`04-tool-use.md`
- 路由用到的 model_hint：`01-gateway-routing.md`
- 权限 scope：`02-auth-and-retry.md`
- 过程记忆沉淀：`06-memory.md`
