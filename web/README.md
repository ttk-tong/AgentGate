# AgentGate 控制台（Web）

Vite + React + TypeScript + Tailwind 的前端控制台，直观展示 AgentGate 的对话、
工具调用、子 agent、上下文压缩等运行过程。

## 功能

- **流式对话**：消费后端 `POST /v1/sessions/{id}/messages/stream` 的 SSE 事件，
  实时渲染 token。
- **事件面板**（右侧）：所有 SSE 事件按到达顺序、分色列出（工具/子 agent/压缩/
  结束/错误），可折叠查看原始 payload。token 事件默认折叠只计数。
- **工具调用可视化**：助手气泡内嵌工具卡片，展示入参与结果，按 ok 着色。
- **dangerous 工具确认**：收到 `tool_confirmation` 事件弹窗，批准/拒绝走
  `POST /v1/sessions/{id}/confirmations` 恢复运行。
- **认证**：顶栏可填 Bearer token（对应后端 API Key），存 localStorage；dev 可留空匿名。

## 本地开发

前置：后端已在 `:8000` 运行（`uvicorn app.main:app --port 8000`）。

```bash
cd web
npm install
npm run dev
```

打开 http://localhost:3000 。开发服务器已配置把 `/v1`、`/healthz`、`/readyz`
反代到 `http://localhost:8000`（见 vite.config.ts），因此**同源、无需关心 CORS**。

> 若 `npm install` 报 esbuild 安装错误（EPERM / 'node' 不是内部命令），是因为
> npm 的子进程 `cmd.exe` 没继承到 node 路径。把 node 所在目录加进 Windows PATH，
> 或在启动 shell 里 `export PATH="<node目录>:$PATH"` 后重装。

## 生产构建

```bash
npm run build     # 产物在 dist/
npm run preview   # 本地预览构建产物
```

分离部署时，把 `dist/` 用任意静态服务器托管，并把 `/v1` 指向真实后端；后端
`CORS_ORIGINS` 需包含前端来源（默认已放行 `http://localhost:3000`）。

## 目录

```
src/
├── api.ts               后端 API 封装（创建会话、流式发消息、确认工具）
├── sse.ts               fetch + ReadableStream 解析 SSE（因流式接口是 POST）
├── types.ts             事件/消息类型，与 app/domain/events.py 对齐
├── App.tsx              主容器：SSE → 聊天视图 + 事件日志的状态机
└── components/
    ├── SessionBar.tsx   顶栏：新建会话 + token
    ├── ChatPanel.tsx    聊天气泡 + 工具卡片
    ├── EventLog.tsx     右侧事件面板
    ├── Composer.tsx     输入框
    └── ConfirmDialog.tsx dangerous 工具确认弹窗
```
