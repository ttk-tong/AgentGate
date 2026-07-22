import { useCallback, useMemo, useRef, useState } from "react";
import {
  confirmTool,
  createSession,
  streamMessage,
  type MessageResp,
} from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { Composer } from "./components/Composer";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { EventLog, type LoggedEvent } from "./components/EventLog";
import { Hero, type Kpi } from "./components/Hero";
import { Manual } from "./components/Manual";
import { SessionBar } from "./components/SessionBar";
import type {
  AgentEvent,
  ChatMessage,
  PendingConfirmation,
  ToolCallView,
} from "./types";

let msgSeq = 0;
const newId = () => `m${Date.now()}-${msgSeq++}`;

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionCount, setSessionCount] = useState(0);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [events, setEvents] = useState<LoggedEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 示例卡预填：nonce 变化触发 Composer 填充
  const [prefill, setPrefill] = useState<{ text: string; nonce: number }>();

  // 当前正在流式接收的助手消息 id（用 ref 避免闭包读到旧 state）
  const activeAsstId = useRef<string | null>(null);

  // 实时 KPI：从 messages / events 派生
  const kpi = useMemo<Kpi>(() => {
    let tools = 0;
    let tokens = 0;
    for (const e of events) {
      if (e.type === "tool_call") tools += 1;
      if (e.type === "usage") {
        tokens +=
          Number(e.data.input_tokens ?? 0) + Number(e.data.output_tokens ?? 0);
      }
    }
    const turns = messages.filter((m) => m.role === "user").length;
    return { sessions: sessionCount, events: events.length, tools, turns, tokens };
  }, [events, messages, sessionCount]);

  const logEvent = useCallback((ev: AgentEvent) => {
    setEvents((prev) => [...prev, { ...ev, ts: Date.now() }]);
  }, []);

  const onNewSession = async (externalUser: string) => {
    setError(null);
    try {
      const { session_id } = await createSession(externalUser);
      setSessionId(session_id);
      setSessionCount((n) => n + 1);
      setMessages([]);
      setEvents([]);
      setPending(null);
    } catch (e) {
      setError(String(e));
    }
  };

  const onTry = (text: string) => setPrefill({ text, nonce: Date.now() });

  // 把单个 SSE 事件应用到助手消息视图上
  const applyEventToChat = useCallback((ev: AgentEvent) => {
    const asstId = activeAsstId.current;
    if (!asstId) return;

    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== asstId) return m;
        switch (ev.type) {
          case "token":
            return { ...m, text: m.text + String(ev.data.text ?? "") };
          case "tool_call": {
            const tc: ToolCallView = {
              toolCallId: String(ev.data.tool_call_id),
              name: String(ev.data.name),
              arguments: (ev.data.arguments as Record<string, unknown>) ?? {},
            };
            return { ...m, toolCalls: [...m.toolCalls, tc] };
          }
          case "tool_result": {
            const id = String(ev.data.tool_call_id);
            return {
              ...m,
              toolCalls: m.toolCalls.map((t) =>
                t.toolCallId === id
                  ? { ...t, ok: Boolean(ev.data.ok), result: ev.data.display }
                  : t,
              ),
            };
          }
          case "usage":
            return {
              ...m,
              usage: {
                input_tokens: Number(ev.data.input_tokens ?? 0),
                output_tokens: Number(ev.data.output_tokens ?? 0),
              },
            };
          case "done":
            return {
              ...m,
              streaming: false,
              stopReason: String(ev.data.stop_reason ?? ""),
            };
          default:
            return m;
        }
      }),
    );
  }, []);

  // 消费一条事件流（发消息 / 确认后恢复都复用）
  const consumeStream = useCallback(
    async (gen: AsyncGenerator<AgentEvent>) => {
      for await (const ev of gen) {
        logEvent(ev);
        applyEventToChat(ev);
        if (ev.type === "tool_confirmation") {
          setPending({
            toolCallId: String(ev.data.tool_call_id),
            name: String(ev.data.name),
            arguments: (ev.data.arguments as Record<string, unknown>) ?? {},
            reason: (ev.data.reason as string | null) ?? null,
          });
        }
        if (ev.type === "error") {
          setError(String(ev.data.message ?? "unknown error"));
        }
      }
    },
    [logEvent, applyEventToChat],
  );

  const onSend = async (text: string) => {
    if (!sessionId) return;
    setError(null);
    setBusy(true);

    // 用户消息 + 占位助手消息
    const asstId = newId();
    activeAsstId.current = asstId;
    setMessages((prev) => [
      ...prev,
      { id: newId(), role: "user", text, toolCalls: [] },
      { id: asstId, role: "assistant", text: "", toolCalls: [], streaming: true },
    ]);

    try {
      await consumeStream(streamMessage(sessionId, text));
    } catch (e) {
      setError(String(e));
      setMessages((prev) =>
        prev.map((m) =>
          m.id === asstId ? { ...m, streaming: false } : m,
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  // 确认/拒绝 dangerous 工具：非流式恢复，把聚合结果补进当前助手消息
  const onDecide = async (approved: boolean) => {
    if (!sessionId || !pending) return;
    setBusy(true);
    try {
      const resp: MessageResp = await confirmTool(
        sessionId,
        pending.toolCallId,
        approved,
      );
      setPending(null);
      logEvent({
        type: "done",
        data: { stop_reason: resp.stop_reason, usage: resp.usage },
        seq: 0,
      });
      const asstId = activeAsstId.current;
      if (asstId) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === asstId
              ? {
                  ...m,
                  text: m.text + (resp.reply || ""),
                  streaming: false,
                  stopReason: resp.stop_reason,
                  usage: resp.usage,
                }
              : m,
          ),
        );
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col bg-console">
      <SessionBar
        sessionId={sessionId}
        onNewSession={onNewSession}
        busy={busy}
      />

      {error && (
        <div className="border-b border-fault/30 bg-fault/10 px-5 py-1.5 text-sm text-fault">
          {error}
        </div>
      )}

      <Hero kpi={kpi} hasSession={!!sessionId} />

      <div className="flex min-h-0 flex-1">
        {/* 左：实时控制台 */}
        <div className="flex min-w-0 flex-1 flex-col">
          <ChatPanel messages={messages} />
          <Composer
            onSend={onSend}
            disabled={!sessionId || busy}
            prefill={prefill}
          />
        </div>
        {/* 中：事件轨道 */}
        <EventLog events={events} onClear={() => setEvents([])} />
        {/* 右：手册（纸色） */}
        <Manual onTry={onTry} disabled={!sessionId || busy} />
      </div>

      {pending && (
        <ConfirmDialog pending={pending} onDecide={onDecide} busy={busy} />
      )}
    </div>
  );
}
