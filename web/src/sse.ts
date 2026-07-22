import type { AgentEvent } from "./types";

// 后端流式接口是 POST（native EventSource 只支持 GET），因此用 fetch + ReadableStream
// 手动解析 SSE 帧。每帧形如：
//   event: token\n
//   data: {"type":"token",...}\n
//   \n
// 我们只依赖 data 行的 JSON（其中已含 type），event 行冗余可忽略。

export async function* parseSSE(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 以空行分隔帧
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const ev = parseFrame(frame);
      if (ev) yield ev;
    }
  }
  // 冲刷尾帧（正常结束时通常已空）
  const tail = parseFrame(buffer);
  if (tail) yield tail;
}

function parseFrame(frame: string): AgentEvent | null {
  const dataLines: string[] = [];
  for (const raw of frame.split("\n")) {
    const line = raw.trimEnd();
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    return JSON.parse(dataLines.join("\n")) as AgentEvent;
  } catch {
    return null;
  }
}
