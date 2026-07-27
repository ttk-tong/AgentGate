import { Component, type ErrorInfo, type ReactNode } from "react";

// 渲染层错误边界：任何子树抛错都收敛为一块可恢复的故障面板，
// 而不是整页白屏。刷新即可恢复（会话在后端，不丢）。
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center gap-4 bg-console p-8 text-center"
      >
        <div className="font-mono text-meta uppercase text-fault">
          render fault
        </div>
        <h1 className="font-display text-h1 font-bold text-ink">
          界面渲染出了点问题
        </h1>
        <p className="max-w-md text-sm leading-relaxed text-dim">
          会话数据都在服务端，刷新页面即可恢复。错误详情已打印到浏览器控制台。
        </p>
        <pre className="max-w-lg overflow-x-auto rounded-md bg-panel p-3 text-left font-mono text-xs text-fault/80">
          {String(this.state.error)}
        </pre>
        <button
          onClick={() => location.reload()}
          className="rounded-md bg-signal px-4 py-1.5 text-sm font-semibold text-console hover:opacity-90"
        >
          刷新页面
        </button>
      </div>
    );
  }
}
