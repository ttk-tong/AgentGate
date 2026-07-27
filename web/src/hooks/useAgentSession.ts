import { useCallback, useMemo, useRef, useState } from "react";
import {
  confirmTool,
  createSession,
  streamMessage,
  type MessageResp,
} from "../api";
import type {
  AgentEvent,
  ChatMessage,
  ConnectionState,
  Kpi,
  LoggedEvent,
  PendingConfirmation,
  ToolCallView,
} from "../types";

// 会话状态机的前端镜像：session / 消息流 / 事件轨道 / 待确认 / 断流恢复
// 全部收在这个 hook 里，App 只负责布局与组装。

let msgSeq = 0;
const newId = () => `m${Date.now()}-${msgSeq++}`;

export interface AgentSession {
  sessionId: string | null;
  messages: ChatMessage[];
  events: LoggedEvent[];
  busy: boolean;
  pending: PendingConfirmation | null;
  error: string | null;
  connection: ConnectionState;
  kpi: Kpi;
  newSession: (externalUser: string) => Promise<void>;
  send: (text: string) => Promise<void>;
  decide: (approved: boolean) => Promise<void>;
  retryLast: () => Promise<void>;
  clearEvents: () => void;
  dismissError: () => void;
}

export function useAgentSession(): AgentSession {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionCount, setSessionCount] = useState(0);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [events, setEvents] = useState<LoggedEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>({
    status: "ok",
  });

  // 当前正在流式接收的助手消息 id（用 ref 避免闭包读到旧 state）
  const activeAsstId = useRef<string | null>(null);
  // 当前回合号：每次用户发送 +1，事件轨道按它分组
  const turnRef = useRef(0);
  // 本次流已收到的事件数：即使 consumeStream 中途抛错也能读到真实进度，
  // 用于判断"重试是否安全"（0 → 请求没跑起来，可重试；>0 → 已有副作用）
  const streamedRef = useRef(0);

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
    setEvents((prev) => [
      ...prev,
      { ...ev, ts: Date.now(), turn: turnRef.current },
    ]);
  }, []);

  const newSession = useCallback(async (externalUser: string) => {
    setError(null);
    setConnection({ status: "ok" });
    try {
      const { session_id } = await createSession(externalUser);
      setSessionId(session_id);
      setSessionCount((n) => n + 1);
      setMessages([]);
      setEvents([]);
      setPending(null);
      turnRef.current = 0;
    } catch (e) {
      setError(String(e));
    }
  }, []);

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

  // 消费一条事件流（发消息 / 确认后恢复都复用）。
  // streamedRef 在每个事件到达时递增，即使 for-await 中途抛错也能反映真实进度，
  // 供断流重试判断"这次尝试是否已经产生副作用"。
  const consumeStream = useCallback(
    async (gen: AsyncGenerator<AgentEvent>): Promise<void> => {
      for await (const ev of gen) {
        streamedRef.current += 1;
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

  const send = useCallback(
    async (text: string) => {
      if (!sessionId) return;
      setError(null);
      setConnection({ status: "ok" });
      setBusy(true);
      turnRef.current += 1;

      // 用户消息 + 占位助手消息
      const asstId = newId();
      activeAsstId.current = asstId;
      setMessages((prev) => [
        ...prev,
        { id: newId(), role: "user", text, toolCalls: [] },
        { id: asstId, role: "assistant", text: "", toolCalls: [], streaming: true },
      ]);

      try {
        // 后端流是一次性 POST，没有断点续传：
        // - 一个事件都没收到 → 请求没跑起来，指数退避安全重试 2 次
        // - 已收到部分事件 → 服务端已在执行，不能盲目重发；标记中断，交给用户一键重发
        streamedRef.current = 0;
        const MAX_RETRY = 2;
        for (let attempt = 0; ; attempt++) {
          try {
            await consumeStream(streamMessage(sessionId, text));
            break;
          } catch (e) {
            if (streamedRef.current === 0 && attempt < MAX_RETRY) {
              await new Promise((r) => setTimeout(r, 800 * 2 ** attempt));
              continue;
            }
            throw e;
          }
        }
      } catch (e) {
        setError(String(e));
        setConnection({ status: "lost", lastInput: text });
        setMessages((prev) =>
          prev.map((m) =>
            m.id === asstId ? { ...m, streaming: false, interrupted: true } : m,
          ),
        );
      } finally {
        setBusy(false);
      }
    },
    [sessionId, consumeStream],
  );

  // 断流后一键重发：新起一条消息重试同样的输入
  const retryLast = useCallback(async () => {
    const text = connection.lastInput;
    if (!text) return;
    await send(text);
  }, [connection.lastInput, send]);

  // 确认/拒绝 dangerous 工具：非流式恢复，把聚合结果补进当前助手消息
  const decide = useCallback(
    async (approved: boolean) => {
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
    },
    [sessionId, pending, logEvent],
  );

  const clearEvents = useCallback(() => setEvents([]), []);
  const dismissError = useCallback(() => setError(null), []);

  return {
    sessionId,
    messages,
    events,
    busy,
    pending,
    error,
    connection,
    kpi,
    newSession,
    send,
    decide,
    retryLast,
    clearEvents,
    dismissError,
  };
}
