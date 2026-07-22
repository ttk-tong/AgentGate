import { useEffect, useRef, useState } from "react";

// 底部输入框：受控组件，支持从「示例卡」预填。Enter 发送，Shift+Enter 换行。
export function Composer({
  onSend,
  disabled,
  prefill,
}: {
  onSend: (text: string) => void;
  disabled: boolean;
  // {text, nonce}：nonce 变化即触发一次填充（同一句可重复填）
  prefill?: { text: string; nonce: number };
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (prefill && prefill.text) {
      setText(prefill.text);
      ref.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);

  const send = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setText("");
  };

  return (
    <div className="border-t border-rule bg-console p-3">
      <div className="flex items-end gap-2">
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={2}
          placeholder={
            disabled
              ? "先「开始会话」再输入…"
              : "说点什么，Enter 发送 · Shift+Enter 换行"
          }
          disabled={disabled}
          className="flex-1 resize-none rounded-md bg-panel px-3 py-2 text-sm text-ink outline-none ring-1 ring-rule transition-shadow placeholder:text-dim focus:ring-signal/60 disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={disabled || !text.trim()}
          className="h-10 rounded-md bg-signal px-5 text-sm font-semibold text-console transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          发送
        </button>
      </div>
    </div>
  );
}
