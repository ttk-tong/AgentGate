import { useEffect, useRef } from "react";
import type { PendingConfirmation } from "../types";

// dangerous 工具确认弹窗：焦点陷阱 + Esc 拒绝。
export function ConfirmDialog({
  pending,
  onDecide,
  busy,
}: {
  pending: PendingConfirmation;
  onDecide: (approved: boolean) => void;
  busy: boolean;
}) {
  const denyRef = useRef<HTMLButtonElement>(null);

  // 打开时聚焦到"拒绝"按钮（安全默认）
  useEffect(() => { denyRef.current?.focus(); }, []);

  // Esc 拒绝；Tab 在两个按钮间循环（焦点陷阱）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onDecide(false); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onDecide]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
      aria-describedby="confirm-desc"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
    >
      <div className="w-[28rem] rounded-lg border border-warn/40 bg-panel p-5 shadow-2xl">
        <div className="mb-1 font-mono text-meta uppercase text-warn">
          confirmation required
        </div>
        <div id="confirm-title" className="mb-2 font-display text-lg font-bold text-ink">
          这个工具需要你放行
        </div>
        <div id="confirm-desc" className="mb-3 text-sm text-dim">
          模型请求执行一个敏感工具。确认后才会真正运行。
        </div>

        <div className="mb-4 rounded-md bg-console p-3 font-mono text-xs">
          <div className="text-ev-tool">{pending.name}</div>
          {pending.reason && (
            <div className="mt-1 text-dim">原因：{pending.reason}</div>
          )}
          <pre className="mt-2 overflow-x-auto text-dim">
            {JSON.stringify(pending.arguments, null, 2)}
          </pre>
        </div>

        <div className="flex justify-end gap-2">
          <button
            ref={denyRef}
            onClick={() => onDecide(false)}
            disabled={busy}
            className="rounded-md border border-rule px-4 py-1.5 text-sm text-dim hover:border-dim hover:text-ink disabled:opacity-50 focus-visible:outline"
          >
            拒绝
          </button>
          <button
            onClick={() => onDecide(true)}
            disabled={busy}
            className="rounded-md bg-warn px-4 py-1.5 text-sm font-semibold text-console hover:opacity-90 disabled:opacity-50 focus-visible:outline"
          >
            批准执行
          </button>
        </div>
      </div>
    </div>
  );
}
