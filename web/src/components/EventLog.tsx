import { useState } from "react";
import type { AgentEvent, EventType } from "../types";

// 右侧观测面板：所有 SSE 事件按到达顺序列出，可折叠 payload。
// 分色对应事件类型，一眼看清工具/子 agent/压缩/结束。

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

export interface LoggedEvent extends AgentEvent {
  ts: number;
}

export function EventLog({
  events,
  onClear,
}: {
  events: LoggedEvent[];
  onClear: () => void;
}) {
  // token 事件量大，默认折叠（只统计条数），聚焦结构化事件。
  const [showTokens, setShowTokens] = useState(false);
  const visible = events.filter((e) => showTokens || e.type !== "token");
  const tokenCount = events.filter((e) => e.type === "token").length;

  return (
    <div className="scroll-dark flex w-96 shrink-0 flex-col border-l border-rule bg-console">
      <div className="flex items-center gap-2 border-b border-rule px-3 py-2.5">
        <span className="font-display text-sm font-semibold text-ink">
          事件轨道
        </span>
        <span className="font-mono text-meta text-dim">{events.length}</span>
        <label className="ml-auto flex items-center gap-1 font-mono text-meta text-dim">
          <input
            type="checkbox"
            checked={showTokens}
            onChange={(e) => setShowTokens(e.target.checked)}
          />
          token（{tokenCount}）
        </label>
        <button
          onClick={onClear}
          className="rounded px-2 py-0.5 font-mono text-meta text-dim hover:text-ink"
        >
          清空
        </button>
      </div>

      <div className="scroll-dark flex-1 overflow-y-auto p-2 font-mono text-xs">
        {visible.length === 0 && (
          <div className="p-4 text-center text-xs leading-relaxed text-dim">
            等第一个 SSE 事件飞进来 —— token、工具调用、子 agent 分叉、上下文压缩
            都会按到达顺序落在这条轨道上。
          </div>
        )}
        {visible.map((e, i) => (
          <EventRow key={i} ev={e} />
        ))}
      </div>
    </div>
  );
}

function EventRow({ ev }: { ev: LoggedEvent }) {
  const [open, setOpen] = useState(false);
  const time = new Date(ev.ts).toLocaleTimeString("zh-CN", { hour12: false });
  const summary = summarize(ev);

  return (
    <div className="border-b border-rule/60 py-1">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 text-left hover:bg-panel/60"
      >
        <span className="text-dim">{time}</span>
        <span className={`font-semibold ${COLOR[ev.type]}`}>{LABEL[ev.type]}</span>
        <span className="flex-1 truncate text-dim">{summary}</span>
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
