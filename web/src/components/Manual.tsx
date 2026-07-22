// 右侧「手册」：纸色底，打破全深色默认。面向访客解释项目是什么、
// 架构长什么样、能试哪些 demo、技术栈。示例 prompt 点击即填 composer。

const HIGHLIGHTS: { title: string; body: string }[] = [
  {
    title: "事件 DAG 会话",
    body: "会话不是线性消息数组，而是带 parent / logical_parent 的事件图。压缩、分支、子 agent 都是图上的节点，可投影成 LLM 消息，也可回放。",
  },
  {
    title: "子 Agent 隔离",
    body: "父 agent 委派子任务时 fork 出隔离上下文，只回传最终文本。中间过程标记为 sidechain，不污染父对话，可只读 fan-out 并行。",
  },
  {
    title: "上下文自动压缩",
    body: "逼近窗口上限时在 compact_boundary 处折叠历史为摘要，释放 token 而保留连续性——不是粗暴截断。",
  },
];

const TRY: { label: string; prompt: string }[] = [
  {
    label: "并行子 agent",
    prompt: "派两个子 agent 分别用一句话总结 REST 和 GraphQL，再合并对比。",
  },
  {
    label: "写入记忆",
    prompt: "记住：以后都用中文、简洁地回答我。",
  },
  {
    label: "触发工具确认",
    prompt: "调用一个敏感工具，让我先确认再执行。",
  },
  {
    label: "触发上下文压缩",
    prompt: "我们来聊一段很长的历史，直到触发上下文压缩，然后告诉我压缩前后 token 变化。",
  },
];

const STACK = [
  "FastAPI",
  "async SQLAlchemy",
  "Postgres",
  "Redis Streams",
  "APScheduler",
  "SSE",
  "React + Vite",
];

export function Manual({
  onTry,
  disabled,
}: {
  onTry: (prompt: string) => void;
  disabled: boolean;
}) {
  return (
    <aside className="scroll-paper hidden w-[22rem] shrink-0 overflow-y-auto border-l border-rule bg-paper text-paper-ink lg:block xl:w-[26rem]">
      <div className="space-y-8 px-6 py-6">
        <Section eyebrow="what it is" title="这是什么">
          <p className="text-sm leading-relaxed text-paper-ink/80">
            AgentGate 是一个自建的 AI Agent 运行时网关。它把「调一次大模型」升级为
            一套可观察、可恢复、可编排的 agent loop —— 显式状态机、命名停止原因、
            工具确认、子 agent、记忆与技能分层。
          </p>
        </Section>

        <Section eyebrow="why it matters" title="三个核心设计">
          <ol className="space-y-4">
            {HIGHLIGHTS.map((h, i) => (
              <li key={h.title} className="border-l-2 border-signal pl-3">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-meta text-paper-dim">
                    0{i + 1}
                  </span>
                  <span className="font-display text-sm font-semibold">
                    {h.title}
                  </span>
                </div>
                <p className="mt-1 text-sm leading-relaxed text-paper-ink/75">
                  {h.body}
                </p>
              </li>
            ))}
          </ol>
        </Section>

        <Section eyebrow="architecture" title="请求怎么流动">
          <pre className="overflow-x-auto rounded-md border border-paper-rule bg-paper-ink/[0.03] p-3 font-mono text-[11px] leading-relaxed text-paper-ink/80">
{`client ─SSE─▶ Agent Loop (状态机)
                │
     ┌──────────┼───────────┐
     ▼          ▼           ▼
 Provider   Tool Registry  Session Store
(LLM 流式)  ├ builtin      (事件 DAG)
            └ spawn_agent      │
                 │         Postgres
                 ▼
            Subagent Runner
            (隔离 · sidechain)

  Redis Streams ◀ Queue ─ 记忆抽取 / 会话固化
  APScheduler   ─ 分布式锁 ─ 定时任务`}
          </pre>
        </Section>

        <Section eyebrow="try these" title="点一下就发给 agent">
          <div className="grid grid-cols-1 gap-2">
            {TRY.map((t) => (
              <button
                key={t.label}
                onClick={() => onTry(t.prompt)}
                disabled={disabled}
                className="group rounded-md border border-paper-rule bg-white/40 px-3 py-2 text-left transition-colors hover:border-paper-ink/40 hover:bg-white/70 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <div className="flex items-center justify-between">
                  <span className="font-display text-sm font-semibold">
                    {t.label}
                  </span>
                  <span className="text-paper-dim transition-transform group-hover:translate-x-0.5">
                    →
                  </span>
                </div>
                <p className="mt-0.5 line-clamp-2 text-xs text-paper-ink/60">
                  {t.prompt}
                </p>
              </button>
            ))}
          </div>
          {disabled && (
            <p className="mt-2 text-xs text-paper-dim">
              先「开始会话」，再点上面任意一条试跑。
            </p>
          )}
        </Section>

        <Section eyebrow="stack" title="技术栈">
          <div className="flex flex-wrap gap-1.5">
            {STACK.map((s) => (
              <span
                key={s}
                className="rounded border border-paper-rule px-2 py-0.5 font-mono text-xs text-paper-ink/70"
              >
                {s}
              </span>
            ))}
          </div>
          <a
            href="https://github.com"
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-flex items-center gap-1.5 font-display text-sm font-semibold text-paper-ink underline decoration-signal decoration-2 underline-offset-4 hover:decoration-paper-ink"
          >
            查看源码与 README →
          </a>
        </Section>
      </div>
    </aside>
  );
}

function Section({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-2 font-mono text-meta uppercase text-paper-dim">
        {eyebrow}
      </div>
      <h2 className="mb-2.5 font-display text-lg font-bold tracking-tight">
        {title}
      </h2>
      {children}
    </section>
  );
}
