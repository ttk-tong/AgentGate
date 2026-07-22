import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发期把后端 API 反代到 8000，避免 CORS。生产部署时前端可与后端同源，
// 或把 /v1 指到真实后端地址。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/v1": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/healthz": "http://localhost:8000",
      "/readyz": "http://localhost:8000",
    },
  },
});
