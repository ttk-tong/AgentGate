/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // —— 双底基调：深色控制台 + 纸色手册 ——
        paper: "#F1EDE4", // 右侧手册背景，打破全深色默认
        console: "#0E1013", // 控制台深底（略暖棕黑）
        panel: "#14171C", // 控制台内层卡片
        rule: "#232630", // 分割线 / border
        ink: "#E8E4DA", // 深底上的主文字
        dim: "#7A7B82", // 次文字
        // —— 信号色：事件在流动 ——
        signal: "#B8FF3D", // 荧光柠檬绿，主 accent
        warn: "#FFB454", // 琥珀：子 agent / 需确认
        fault: "#FF5D62", // 错误
        // 纸色区文字
        "paper-ink": "#1A1712",
        "paper-dim": "#726A5A",
        "paper-rule": "#D8D1C2",
        // 事件类型配色（timeline / eventlog 复用）
        ev: {
          token: "#7A7B82",
          tool: "#5B9CFF",
          result: "#B8FF3D",
          error: "#FF5D62",
          compact: "#B08CFF",
          sidechain: "#FFB454",
          done: "#7A7B82",
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
