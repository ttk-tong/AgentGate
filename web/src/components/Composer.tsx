import { useEffect, useRef, useState } from "react";

export function Composer({
  onSend,
  disabled,
  prefill,
}: {
  onSend: (text: string) => void;
  disabled: boolean;
  prefill?: { text: string; nonce: number };
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (prefill?.text) {
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
        <label htmlFor="composer-input" className="sr-only">
          输入消息
        </label>
        <textarea
          id="composer-input"
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
          aria-disabled={disabled}
          className="flex-1 resize-none rounded-md bg-panel px-3 py-2 text-sm text-ink outline-none ring-1 ring-rule transition-shadow placeholder:text-dim focus:ring-signal/60 disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={disabled || !text.trim()}
          aria-label="发送消息"
          className="h-10 rounded-md bg-signal px-5 text-sm font-semibold text-console transition-opacity hover:opacity-90 disabled:opacity-40 focus-visible:outline"
        >
          发送
        </button>
      </div>
    </div>
  );
}
