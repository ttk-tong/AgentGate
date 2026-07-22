// 与后端 app/domain/events.py 的 Event 协议对齐。
// SSE 帧格式：`event: <type>\ndata: <json>\n\n`（见 chat.py 的 _sse）。

export type EventType =
  | "token"
  | "tool_call"
  | "tool_result"
  | "tool_confirmation"
  | "usage"
  | "done"
  | "error"
  | "compact"
  | "subagent";

export interface AgentEvent {
  type: EventType;
  data: Record<string, unknown>;
  seq: number;
}

// 前端聊天视图里的一条消息（用户/助手/工具卡片）
export type ChatRole = "user" | "assistant";

export interface ToolCallView {
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  ok?: boolean;
  result?: unknown; // tool_result 的 display
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  // 该助手回合里触发的工具调用（按 tool_call_id 聚合，保持发生顺序）
  toolCalls: ToolCallView[];
  streaming?: boolean;
  stopReason?: string;
  usage?: { input_tokens?: number; output_tokens?: number };
}

// 待确认的 dangerous 工具（收到 tool_confirmation 事件时弹窗）
export interface PendingConfirmation {
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  reason: string | null;
}
