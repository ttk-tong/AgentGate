/** @type {import('tailwindcss').Config} */

// 所有语义色走 CSS 变量（RGB 三元组，见 index.css），
// 深/浅色只切换变量值，组件类名不变。`<alpha-value>` 保住 bg-signal/15 这类透明度写法。
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        // —— 双底基调：控制台 + 纸色手册 ——
        paper: v("paper"),
        console: v("console"),
        panel: v("panel"),
        rule: v("rule"),
        ink: v("ink"),
        dim: v("dim"),
        // —— 信号色：事件在流动 ——
        signal: v("signal"),
        warn: v("warn"),
        fault: v("fault"),
        // 纸色区文字
        "paper-ink": v("paper-ink"),
        "paper-dim": v("paper-dim"),
        "paper-rule": v("paper-rule"),
        // 事件类型配色（timeline / eventlog 复用）
        ev: {
          token: v("ev-token"),
          tool: v("ev-tool"),
          result: v("ev-result"),
          error: v("ev-error"),
          compact: v("ev-compact"),
          sidechain: v("ev-sidechain"),
          done: v("ev-done"),
        },
      },
      fontFamily: {
        display: ['"Space Grotesk"', "system-ui", "sans-serif"],
        sans: [
          '"Inter"',
          "system-ui",
          "-apple-system",
          '"Segoe UI"',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          "sans-serif",
        ],
        mono: [
          '"Berkeley Mono"',
          '"JetBrains Mono"',
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      fontSize: {
        // 严格 4 级字号阶
        hero: ["3.5rem", { lineHeight: "1.05", letterSpacing: "-0.02em" }],
        h1: ["1.75rem", { lineHeight: "1.2", letterSpacing: "-0.01em" }],
        meta: ["0.75rem", { lineHeight: "1.33", letterSpacing: "0.06em" }],
      },
      keyframes: {
        pulseSignal: {
          "0%,100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
        fadeUp: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pulseSignal: "pulseSignal 1.6s ease-in-out infinite",
        fadeUp: "fadeUp 0.35s ease-out both",
      },
    },
  },
  plugins: [],
};
