import type { ChatMessage, ToolCallView } from "../types";

// 聊天主区：用户/助手气泡 + 助手回合里的工具调用卡片。
export function ChatPanel({ messages }: { messages: ChatMessage[] }) {
  return (
    <div className="scroll-dark flex flex-1 flex-col gap-4 overflow-y-auto p-4">
      {messages.length === 0 && (
        <div className="m-auto max-w-sm text-center">
          <div className="font-mono text-meta uppercase text-dim">idle</div>
          <p className="mt-2 text-sm leading-relaxed text-dim">
            还没有对话。发一条消息，或点右侧「点一下就发给 agent」里的示例 ——
            token、工具调用、子 agent 分叉都会实时落在这里和事件面板上。
          </p>
        </div>
      )}
      {messages.map((m) => (
        <MessageBubble key={m.id} msg={m} />
      ))}
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} animate-fadeUp`}>
      <div
        className={`max-w-[80%] rounded-lg px-4 py-2.5 ${
          isUser
            ? "bg-signal/15 text-ink ring-1 ring-signal/30"
            : "bg-panel text-ink ring-1 ring-rule"
        }`}
      >
        <div className="mb-1 font-mono text-meta uppercase text-dim">
          {isUser ? "you" : "agent"}
        </div>
        {msg.text && (
          <div className="whitespace-pre-wrap break-words text-sm leading-relaxed">
            {msg.text}
          </div>
        )}
        {msg.streaming && !msg.text && (
          <div className="flex items-center gap-1.5 text-sm text-dim">
            <span className="h-1.5 w-1.5 animate-pulseSignal rounded-full bg-signal" />
            思考中
          </div>
        )}

        {msg.toolCalls.map((tc) => (
          <ToolCard key={tc.toolCallId} tc={tc} />
        ))}

        {msg.stopReason && (
          <div className="mt-2 border-t border-rule pt-1.5 font-mono text-meta text-dim">
            stop · {msg.stopReason}
            {msg.usage &&
              ` · in ${msg.usage.input_tokens ?? 0} / out ${msg.usage.output_tokens ?? 0}`}
          </div>
        )}
      </div>
    </div>
  );
}

function ToolCard({ tc }: { tc: ToolCallView }) {
  const pending = tc.ok === undefined;
  const border = pending
    ? "border-ev-tool/50"
    : tc.ok
      ? "border-signal/50"
      : "border-fault/50";
  return (
    <div className={`mt-2 rounded-md border ${border} bg-console/80 p-2`}>
      <div className="flex items-center gap-2 font-mono text-xs">
        <span className="font-semibold text-ev-tool">⌁ {tc.name}</span>
        <span className="text-dim">
          {pending ? "running…" : tc.ok ? "✓ ok" : "✗ failed"}
        </span>
      </div>
      {Object.keys(tc.arguments).length > 0 && (
        <pre className="mt-1 overflow-x-auto font-mono text-xs text-dim">
          {JSON.stringify(tc.arguments, null, 2)}
        </pre>
      )}
      {tc.result !== undefined && (
        <pre className="mt-1 overflow-x-auto border-t border-rule pt-1 font-mono text-xs text-ink/80">
          {typeof tc.result === "string"
            ? tc.result
            : JSON.stringify(tc.result, null, 2)}
        </pre>
      )}
    </div>
  );
}
