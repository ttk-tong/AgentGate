// 主题切换：localStorage 显式选择优先，否则跟随系统 prefers-color-scheme。
// index.html 里有一段首帧内联脚本做同样的判断以避免闪烁（FOUC），此处保持逻辑一致。

export type Theme = "dark" | "light";

const THEME_KEY = "agentgate.theme";

export function resolveTheme(): Theme {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
}

/** 跟随系统变化（仅在用户未显式选择时生效）。返回取消订阅函数。 */
export function watchSystemTheme(onChange: (t: Theme) => void): () => void {
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const handler = (e: MediaQueryListEvent) => {
    if (localStorage.getItem(THEME_KEY)) return; // 用户已手动选择
    const t: Theme = e.matches ? "light" : "dark";
    applyTheme(t);
    onChange(t);
  };
  mq.addEventListener("change", handler);
  return () => mq.removeEventListener("change", handler);
}
