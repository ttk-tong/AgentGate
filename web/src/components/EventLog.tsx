import { useEffect, useRef, useState } from "react";
import type { EventType, LoggedEvent } from "../types";

// 事件轨道：按回合分组，类型过滤 chips，可展开工具入参/结果，实时 token 计量条。

const COLOR: Record<EventType, string> = {
  token: "text-dim",
  tool_call: "text-ev-tool",
  tool_result: "text-signal",
  tool_confirmation: "text-warn",
  usage: "text-dim",
  done: "text-dim",
  error: "text-fault",
  compact: "text-ev-compact",
  subagent: "text-warn",
};

const LABEL: Record<EventType, string> = {
  token: "token",
  tool_call: "工具调用",
  tool_result: "工具结果",
  tool_confirmation: "待确认",
  usage: "用量",
  done: "结束",
  error: "错误",
  compact: "压缩",
  subagent: "子agent",
};

// 所有可过滤的类型（token 单独控制）
const FILTER_TYPES: EventType[] = [
  "tool_call",
  "tool_result",
  "tool_confirmation",
  "usage",
  "done",
  "error",
  "compact",
  "subagent",
];

export function EventLog({
  events,
  onClear,
  busy,
}: {
  events: LoggedEvent[];
  onClear: () => void;
  busy: boolean;
}) {
  const [showTokens, setShowTokens] = useState(false);
  const [hiddenTypes, setHiddenTypes] = useState<Set<EventType>>(new Set());
  const bottomRef = useRef<HTMLDivElement>(null);

  // 自动吸底：新事件到来时滚到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);

  const toggleType = (t: EventType) =>
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });

  const visible = events.filter(
    (e) =>
      (showTokens || e.type !== "token") && !hiddenTypes.has(e.type),
  );

  // 按回合分组
  const groups = groupByTurn(visible);

  // 实时 token 计量：累计 output_tokens
  const totalOut = events
    .filter((e) => e.type === "usage")
    .reduce((s, e) => s + Number(e.data.output_tokens ?? 0), 0);
  const totalIn = events
    .filter((e) => e.type === "usage")
    .reduce((s, e) => s + Number(e.data.input_tokens ?? 0), 0);

  return (
    <section
      aria-label="事件轨道"
      className="scroll-dark flex w-96 shrink-0 flex-col border-l border-rule bg-console"
    >
      {/* 头部 */}
      <div className="flex flex-col gap-1.5 border-b border-rule px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="font-display text-sm font-semibold text-ink">
            事件轨道
          </span>
          {busy && (
            <span
              aria-label="流式接收中"
              className="h-1.5 w-1.5 animate-pulseSignal rounded-full bg-signal"
            />
          )}
          <span className="font-mono text-meta text-dim" aria-live="polite">
            {events.length}
          </span>
          <button
            onClick={onClear}
            aria-label="清空事件轨道"
            className="ml-auto rounded px-2 py-0.5 font-mono text-meta text-dim hover:text-ink focus-visible:outline"
          >
            清空
          </button>
        </div>

        {/* Token 计量条 */}
        {(totalIn > 0 || totalOut > 0) && (
          <div className="flex items-center gap-2 font-mono text-meta text-dim">
            <span>in {totalIn.toLocaleString()}</span>
            <span className="text-rule">·</span>
            <span className="text-signal">out {totalOut.toLocaleString()}</span>
          </div>
        )}

        {/* 类型过滤 chips */}
        <div className="flex flex-wrap gap-1" role="group" aria-label="事件类型过滤">
          <button
            onClick={() => setShowTokens((s) => !s)}
            aria-pressed={showTokens}
            className={`rounded-full border px-2 py-0.5 font-mono text-meta transition-colors focus-visible:outline ${
              showTokens
                ? "border-dim text-ink"
                : "border-rule text-dim hover:border-dim"
            }`}
          >
            token
          </button>
          {FILTER_TYPES.map((t) => {
            const active = !hiddenTypes.has(t);
            return (
              <button
                key={t}
                onClick={() => toggleType(t)}
                aria-pressed={active}
                className={`rounded-full border px-2 py-0.5 font-mono text-meta transition-colors focus-visible:outline ${
                  active
                    ? `border-current ${COLOR[t]}`
                    : "border-rule text-dim hover:border-dim"
                }`}
              >
                {LABEL[t]}
              </button>
            );
          })}
        </div>
      </div>

      {/* 事件列表 */}
      <div
        className="scroll-dark flex-1 overflow-y-auto p-2 font-mono text-xs"
        role="log"
        aria-live="polite"
        aria-label="实时事件流"
      >
        {visible.length === 0 && (
          <div className="p-4 text-center text-xs leading-relaxed text-dim">
            等第一个 SSE 事件飞进来 —— token、工具调用、子 agent 分叉、上下文压缩
            都会按到达顺序落在这条轨道上。
          </div>
        )}
        {groups.map(({ turn, items }) => (
          <TurnGroup key={turn} turn={turn} items={items} />
        ))}
        <div ref={bottomRef} />
      </div>
    </section>
  );
}

function TurnGroup({ turn, items }: { turn: number; items: LoggedEvent[] }) {
  return (
    <div className="mb-2">
      <div className="mb-1 flex items-center gap-1.5">
        <span className="font-mono text-meta uppercase text-dim/60">
          turn {turn}
        </span>
        <span className="h-px flex-1 bg-rule/60" />
      </div>
      {items.map((e, i) => (
        <EventRow key={i} ev={e} />
      ))}
    </div>
  );
}

function EventRow({ ev }: { ev: LoggedEvent }) {
  const [open, setOpen] = useState(false);
  const time = new Date(ev.ts).toLocaleTimeString("zh-CN", { hour12: false });
  const summary = summarize(ev);
  const hasDetail =
    ev.type === "tool_call" ||
    ev.type === "tool_result" ||
    ev.type === "error" ||
    ev.type === "compact" ||
    ev.type === "subagent";

  return (
    <div className="border-b border-rule/40 py-0.5">
      <button
        onClick={() => hasDetail && setOpen((o) => !o)}
        aria-expanded={hasDetail ? open : undefined}
        className={`flex w-full items-start gap-2 rounded px-1 py-0.5 text-left ${
          hasDetail ? "hover:bg-panel/60 focus-visible:outline" : "cursor-default"
        }`}
      >
        <span className="shrink-0 text-dim">{time}</span>
        <span className={`shrink-0 font-semibold ${COLOR[ev.type]}`}>
          {LABEL[ev.type]}
        </span>
        <span className="min-w-0 flex-1 truncate text-dim">{summary}</span>
        {hasDetail && (
          <span className="shrink-0 text-dim/50">{open ? "▾" : "▸"}</span>
        )}
      </button>
      {open && (
        <pre className="mt-1 overflow-x-auto rounded bg-panel p-2 text-ink/80">
          {JSON.stringify(ev.data, null, 2)}
        </pre>
      )}
    </div>
  );
}

function summarize(ev: LoggedEvent): string {
  const d = ev.data;
  switch (ev.type) {
    case "token":
      return String(d.text ?? "");
    case "tool_call":
      return `${d.name}(${JSON.stringify(d.arguments ?? {})})`;
    case "tool_result":
      return `${d.name} → ${d.ok ? "ok" : "err"}`;
    case "tool_confirmation":
      return `${d.name}：${d.reason ?? "需确认"}`;
    case "usage":
      return `in ${d.input_tokens ?? 0} / out ${d.output_tokens ?? 0}`;
    case "compact":
      return `${d.layer ?? ""} 释放 ${d.freed_tokens ?? 0} tokens`;
    case "done":
      return `stop=${d.stop_reason ?? ""}`;
    case "error":
      return String(d.message ?? "");
    case "subagent":
      return JSON.stringify(d);
    default:
      return "";
  }
}

function groupByTurn(
  events: LoggedEvent[],
): { turn: number; items: LoggedEvent[] }[] {
  const map = new Map<number, LoggedEvent[]>();
  for (const e of events) {
    const arr = map.get(e.turn) ?? [];
    arr.push(e);
    map.set(e.turn, arr);
  }
  return Array.from(map.entries())
    .sort(([a], [b]) => a - b)
    .map(([turn, items]) => ({ turn, items }));
}
