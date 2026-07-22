// 首屏 Hero：一句话定位 + 实时 KPI ribbon。
// KPI 不是装饰——它让访客一眼看出"这是个正在运行的运行时"，而非静态截图。

export interface Kpi {
  sessions: number;
  events: number;
  tools: number;
  turns: number;
  tokens: number;
}

export function Hero({ kpi, hasSession }: { kpi: Kpi; hasSession: boolean }) {
  return (
    <section className="border-b border-rule bg-console px-6 py-7">
      <div className="flex items-start gap-3">
        {/* 信号竖条：呼应"事件在流动" */}
        <span
          className={`mt-1.5 h-14 w-1 shrink-0 rounded-full bg-signal ${
            hasSession ? "animate-pulseSignal" : "opacity-40"
          }`}
        />
        <div>
          <h1 className="font-display text-h1 font-bold text-ink sm:text-hero">
            把 LLM 调用变成
            <br className="hidden sm:block" />
            可观察的 Agent Runtime。
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-dim">
            事件 DAG 会话模型 · 子 Agent 隔离 fan-out · 上下文自动压缩 ·
            跨会话记忆持久化。每一步编排都在下方实时可见。
          </p>
        </div>
      </div>

      {/* KPI ribbon */}
      <dl className="mt-6 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-5">
        <KpiCell label="sessions" value={kpi.sessions} />
        <KpiCell label="events" value={kpi.events} />
        <KpiCell label="tool calls" value={kpi.tools} />
        <KpiCell label="turns" value={kpi.turns} />
        <KpiCell label="tokens" value={kpi.tokens} accent />
      </dl>
    </section>
  );
}

function KpiCell({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent?: boolean;
}) {
  return (
    <div className="bg-console px-4 py-3">
      <dt className="font-mono text-meta uppercase text-dim">{label}</dt>
      <dd
        className={`mt-1 font-display text-2xl font-semibold tabular-nums ${
          accent ? "text-signal" : "text-ink"
        }`}
      >
        {value.toLocaleString()}
      </dd>
    </div>
  );
}
