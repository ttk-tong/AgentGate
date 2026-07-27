import { useEffect, useRef, useState } from "react";
import { checkHealth, getToken, setToken } from "../api";
import { resolveTheme, setTheme, watchSystemTheme, type Theme } from "../theme";

export function SessionBar({
  sessionId,
  onNewSession,
  busy,
}: {
  sessionId: string | null;
  onNewSession: (externalUser: string) => void;
  busy: boolean;
}) {
  const [token, setTok] = useState(getToken());
  const [showToken, setShowToken] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [theme, setThemeState] = useState<Theme>(resolveTheme);
  const drawerRef = useRef<HTMLDivElement>(null);
  const settingsBtnRef = useRef<HTMLButtonElement>(null);

  // 每 15s 探一次后端 /healthz
  useEffect(() => {
    let alive = true;
    const ping = async () => {
      const ok = await checkHealth();
      if (alive) setHealthy(ok);
    };
    ping();
    const t = setInterval(ping, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // 跟随系统主题变化
  useEffect(() => watchSystemTheme(setThemeState), []);

  // 点抽屉外收起；Esc 收起
  useEffect(() => {
    if (!settingsOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (drawerRef.current && !drawerRef.current.contains(e.target as Node))
        setSettingsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setSettingsOpen(false); settingsBtnRef.current?.focus(); }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [settingsOpen]);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    setThemeState(next);
  };

  const healthLabel = healthy === null ? "探测中" : healthy ? "在线" : "离线";
  const healthColor =
    healthy === null ? "bg-dim" : healthy ? "bg-signal animate-pulseSignal" : "bg-warn";

  return (
    <header className="flex items-center gap-4 border-b border-rule bg-console px-5 py-3">
      {/* 品牌区 */}
      <div className="flex items-baseline gap-2">
        <span className="font-display text-lg font-bold tracking-tight text-ink">
          AgentGate
        </span>
        <span className="font-mono text-meta uppercase text-dim">agent runtime</span>
      </div>

      {/* 健康灯 */}
      <div
        className="flex items-center gap-1.5"
        title={`后端 ${healthLabel}`}
        aria-label={`后端状态：${healthLabel}`}
      >
        <span
          aria-hidden="true"
          className={`h-2 w-2 rounded-full ${healthColor}`}
        />
        <span className="font-mono text-meta uppercase text-dim">{healthLabel}</span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        {sessionId && (
          <span className="font-mono text-meta text-dim" aria-label={`当前会话 ${sessionId.slice(0, 8)}`}>
            session {sessionId.slice(0, 8)}
          </span>
        )}

        {/* 深浅色切换 */}
        <button
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "切换为浅色模式" : "切换为深色模式"}
          title={theme === "dark" ? "切换为浅色模式" : "切换为深色模式"}
          className="rounded-md border border-rule px-2.5 py-1.5 text-sm text-dim transition-colors hover:border-dim hover:text-ink focus-visible:outline"
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>

        {/* 设置抽屉：登录密钥 */}
        <div className="relative" ref={drawerRef}>
          <button
            ref={settingsBtnRef}
            onClick={() => setSettingsOpen((s) => !s)}
            aria-expanded={settingsOpen}
            aria-haspopup="dialog"
            className="rounded-md border border-rule px-2.5 py-1.5 text-sm text-dim transition-colors hover:border-dim hover:text-ink focus-visible:outline"
          >
            密钥{token ? " ·" : ""}
          </button>
          {settingsOpen && (
            <div
              role="dialog"
              aria-label="登录密钥设置"
              className="absolute right-0 top-full z-30 mt-2 w-72 rounded-md border border-rule bg-panel p-3 shadow-xl"
            >
              <label
                htmlFor="api-token"
                className="mb-1.5 block font-mono text-meta uppercase text-dim"
              >
                登录密钥（可选）
              </label>
              <div className="flex gap-1">
                <input
                  id="api-token"
                  type={showToken ? "text" : "password"}
                  value={token}
                  onChange={(e) => { setTok(e.target.value); setToken(e.target.value); }}
                  placeholder="本地调试可留空"
                  className="min-w-0 flex-1 rounded bg-console px-2 py-1.5 text-sm text-ink outline-none ring-1 ring-rule focus:ring-signal/60"
                />
                <button
                  onClick={() => setShowToken((s) => !s)}
                  aria-label={showToken ? "隐藏密钥" : "显示密钥"}
                  className="rounded px-2 text-xs text-dim hover:text-ink focus-visible:outline"
                >
                  {showToken ? "隐藏" : "显示"}
                </button>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-dim">
                对应后端 API Key。留空则以匿名身份连接（开发模式）。
              </p>
            </div>
          )}
        </div>

        <button
          onClick={() => onNewSession("demo-user")}
          disabled={busy}
          aria-label={sessionId ? "重新开始一个新会话" : "开始新会话"}
          className="rounded-md bg-signal px-4 py-1.5 text-sm font-semibold text-console transition-opacity hover:opacity-90 disabled:opacity-40 focus-visible:outline"
        >
          {sessionId ? "重开会话" : "开始会话"}
        </button>
      </div>
    </header>
  );
}
