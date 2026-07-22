import { parseSSE } from "./sse";
import type { AgentEvent } from "./types";

// 后端 API 封装。开发期经 vite proxy 反代到 :8000，故用相对路径。
// Bearer token 存 localStorage（对应 Stage 4 的 API Key 认证；dev 可留空匿名）。

const TOKEN_KEY = "agentgate.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t: string): void {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export interface CreateSessionResp {
  session_id: string;
}

// 健康探活：顶栏状态灯用。/healthz 由 vite proxy 反代到 :8000。
export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch("/healthz", { method: "GET" });
    return res.ok;
  } catch {
    return false;
  }
}

export async function createSession(
  externalUser?: string,
): Promise<CreateSessionResp> {
  const res = await fetch("/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ external_user: externalUser || null }),
  });
  if (!res.ok) throw new Error(`创建会话失败: ${res.status} ${await res.text()}`);
  return res.json();
}

// 流式发消息：返回 AgentEvent 异步迭代器。
export async function* streamMessage(
  sessionId: string,
  content: string,
): AsyncGenerator<AgentEvent> {
  const res = await fetch(`/v1/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ content }),
  });
  if (!res.ok || !res.body) {
    throw new Error(`发送失败: ${res.status} ${await res.text()}`);
  }
  yield* parseSSE(res.body);
}

// 确认/拒绝 dangerous 工具后恢复运行（非流式，返回聚合结果）。
export interface MessageResp {
  session_id: string;
  reply: string;
  stop_reason: string;
  head_event_id: string | null;
  usage: Record<string, number>;
  tool_calls: Array<Record<string, unknown>>;
}

export async function confirmTool(
  sessionId: string,
  toolCallId: string,
  approved: boolean,
): Promise<MessageResp> {
  const res = await fetch(`/v1/sessions/${sessionId}/confirmations`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ tool_call_id: toolCallId, approved }),
  });
  if (!res.ok) throw new Error(`确认失败: ${res.status} ${await res.text()}`);
  return res.json();
}
