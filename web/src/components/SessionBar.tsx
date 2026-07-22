import { useEffect, useRef, useState } from "react";
import { checkHealth, getToken, setToken } from "../api";

// 顶栏：品牌 + 健康灯 + 新建会话（主操作）。登录密钥折进设置抽屉，
// 不与主操作平权。文案面向访客，而非系统内部术语。
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
  const drawerRef = useRef<HTMLDivElement>(null);

  // 每 15s 探一次后端 /healthz。
  useEffect(() => {
    let alive = true;
    const ping = async () => {
      const ok = await checkHealth();
      if (alive) setHealthy(ok);
    };
    ping();
    const t = setInterval(ping, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // 点抽屉外收起
  useEffect(() => {
    if (!settingsOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
        setSettingsOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [settingsOpen]);

  const healthLabel =
    healthy === null ? "探测中" : healthy ? "在线" : "离线";
  const healthColor =
    healthy === null
      ? "bg-dim"
      : healthy
        ? "bg-signal animate-pulseSignal"
        : "bg-warn";

  return (
    <header className="flex items-center gap-4 border-b border-rule bg-console px-5 py-3">
      {/* 品牌区 */}
      <div className="flex items-baseline gap-2">
        <span className="font-display text-lg font-bold tracking-tight text-ink">
          AgentGate
        </span>
        <span className="font-mono text-meta uppercase text-dim">
          agent runtime
        </span>
      </div>

      {/* 健康灯 */}
      <div className="flex items-center gap-1.5" title={`后端 ${healthLabel}`}>
        <span className={`h-2 w-2 rounded-full ${healthColor}`} />
        <span className="font-mono text-meta uppercase text-dim">
          {healthLabel}
        </span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        {sessionId && (
          <span className="font-mono text-meta text-dim">
            session {sessionId.slice(0, 8)}
          </span>
        )}

        {/* 设置抽屉：登录密钥 */}
        <div className="relative" ref={drawerRef}>
          <button
            onClick={() => setSettingsOpen((s) => !s)}
            className="rounded-md border border-rule px-2.5 py-1.5 text-sm text-dim transition-colors hover:border-dim hover:text-ink"
            title="设置登录密钥"
          >
            密钥{token ? " ·" : ""}
          </button>
          {settingsOpen && (
            <div className="absolute right-0 top-full z-30 mt-2 w-72 rounded-md border border-rule bg-panel p-3 shadow-xl">
              <label className="mb-1.5 block font-mono text-meta uppercase text-dim">
                登录密钥（可选）
              </label>
              <div className="flex gap-1">
                <input
                  type={showToken ? "text" : "password"}
                  value={token}
                  onChange={(e) => {
                    setTok(e.target.value);
                    setToken(e.target.value);
                  }}
                  placeholder="本地调试可留空"
                  className="min-w-0 flex-1 rounded bg-console px-2 py-1.5 text-sm text-ink outline-none ring-1 ring-rule focus:ring-signal/60"
                />
                <button
                  onClick={() => setShowToken((s) => !s)}
                  className="rounded px-2 text-xs text-dim hover:text-ink"
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
          className="rounded-md bg-signal px-4 py-1.5 text-sm font-semibold text-console transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {sessionId ? "重开会话" : "开始会话"}
        </button>
      </div>
    </header>
  );
}
