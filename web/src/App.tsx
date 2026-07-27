import { ChatPanel } from "./components/ChatPanel";
import { Composer } from "./components/Composer";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { EventLog } from "./components/EventLog";
import { Hero } from "./components/Hero";
import { Manual } from "./components/Manual";
import { SessionBar } from "./components/SessionBar";
import { useAgentSession } from "./hooks/useAgentSession";
import { useState } from "react";

export default function App() {
  const s = useAgentSession();
  // 示例卡预填：nonce 变化触发 Composer 填充
  const [prefill, setPrefill] = useState<{ text: string; nonce: number }>();
  const onTry = (text: string) => setPrefill({ text, nonce: Date.now() });

  const disabled = !s.sessionId || s.busy;

  return (
    <div className="flex h-full flex-col bg-console">
      <SessionBar
        sessionId={s.sessionId}
        onNewSession={s.newSession}
        busy={s.busy}
      />

      {s.error && (
        <div
          role="alert"
          className="flex items-center gap-3 border-b border-fault/30 bg-fault/10 px-5 py-1.5 text-sm text-fault"
        >
          <span className="min-w-0 flex-1 truncate">{s.error}</span>
          <button
            onClick={s.dismissError}
            aria-label="关闭错误提示"
            className="shrink-0 rounded px-1.5 text-fault/70 hover:text-fault"
          >
            ✕
          </button>
        </div>
      )}

      {s.connection.status === "lost" && (
        <div
          role="alert"
          className="flex items-center gap-3 border-b border-warn/30 bg-warn/10 px-5 py-1.5 text-sm text-warn"
        >
          <span className="min-w-0 flex-1 truncate">
            与运行时的连接中断，上一条消息可能未完成。
          </span>
          <button
            onClick={s.retryLast}
            disabled={s.busy}
            className="shrink-0 rounded-md border border-warn/40 px-2.5 py-0.5 text-xs font-semibold text-warn hover:bg-warn/10 disabled:opacity-40"
          >
            重新发送
          </button>
        </div>
      )}

      <Hero kpi={s.kpi} hasSession={!!s.sessionId} />

      <main className="flex min-h-0 flex-1">
        {/* 左：实时控制台 */}
        <section
          aria-label="对话"
          className="flex min-w-0 flex-1 flex-col"
        >
          <ChatPanel messages={s.messages} />
          <Composer onSend={s.send} disabled={disabled} prefill={prefill} />
        </section>
        {/* 中：事件轨道 */}
        <EventLog events={s.events} onClear={s.clearEvents} busy={s.busy} />
        {/* 右：手册（纸色） */}
        <Manual onTry={onTry} disabled={disabled} />
      </main>

      {s.pending && (
        <ConfirmDialog pending={s.pending} onDecide={s.decide} busy={s.busy} />
      )}
    </div>
  );
}
