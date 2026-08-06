"""应用配置。所有配置来自环境变量 / .env，集中在此，禁止散落 os.getenv。"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # —— 运行环境 ——
    app_env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = False  # 生产建议 true（结构化 JSON 日志）
    # dev 便利：启动时自动 alembic upgrade head 建/升表。仅 app_env=dev 生效，
    # 生产必须留 false、走手动迁移（可控可审计）。
    auto_migrate: bool = True
    # 允许跨域的前端来源（逗号分隔）。dev 默认放行 Vite 开发端口；生产按需收紧。
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # —— 存储 ——
    database_url: str = Field(
        default="postgresql+asyncpg://agentgate:agentgate@localhost:5432/agentgate"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    # —— 连接池与韧性（压测暴露：默认池 15 在并发下耗尽，Redis 抖动会阻塞主链路）——
    db_pool_size: int = 20
    db_max_overflow: int = 40
    db_pool_timeout_s: float = 10.0
    # 限流/熔断的 Redis 调用超时；超时即快速失败（fail-open），不拖垮主链路。
    redis_timeout_s: float = 0.5

    # —— OpenTelemetry Tracing（metrics/logs 之外的第三支柱）——
    # otel_enabled=true 且配置 OTLP 端点时，HTTP 请求与 AgentLoop 各阶段
    # （PRE_CALL/LLM_CALL/TOOL_EXEC）打成 span 导出（Jaeger/Tempo）。
    otel_enabled: bool = False
    otel_service_name: str = "agentgate"
    # OTLP HTTP 端点（如 http://jaeger:4318）；留空则只建 provider 不导出。
    otel_exporter_otlp_endpoint: str = ""

    # —— Provider ——
    anthropic_api_key: str = ""
    # OpenAI 兼容端点（如 DeepSeek 代理）：base_url 需含 /v1，走 /chat/completions
    openai_base_url: str = ""
    openai_api_key: str = ""
    default_model: str = "claude-opus-4-8"
    default_system_prompt: str = "你是 AgentGate，一个有帮助的 AI 助手。"
    # 全量摘要压缩用的低成本模型（plan/05 §7.3）；留空则复用主模型。
    summary_model: str = ""
    # 过载时的模型降级链（逗号分隔，按序尝试）；留空则无降级（plan/03 §5）。
    fallback_models: str = ""

    # —— 认证/鉴权（plan/02）——
    # API Key 哈希盐（服务端机密，不入库）。生产必须设置，dev 留默认。
    auth_salt: str = "dev-insecure-salt-change-me"
    # 是否强制认证。dev 默认关闭，方便本地无 key 调试；生产应设 true。
    auth_required: bool = False

    # —— 记忆 / 技能 / 提示词分层（plan/06、07、08）——
    # 是否启用长期记忆召回与写入（remember 工具 + 召回注入 prompt）。
    memory_enabled: bool = True
    # 技能目录（扫描 SKILL.md）；留空则不加载任何技能。
    skills_dir: str = ""
    # —— MCP（外部工具接入，见 app/mcp）——
    # MCP server 接入配置：一段 JSON 数组，格式见 app/mcp/config.py 的模块文档。
    # 留空则整个 MCP 子系统不启用（默认路径，零开销）。
    # 注意 readonly_tools 是唯一能让 MCP 工具进入并发批的开关——并发执行第三方
    # 写操作的后果由部署方承担，所以必须显式声明，不从 server 的 hint 推断。
    mcp_servers: str = ""
    # Agent 身份（提示词 identity 块）。
    agent_name: str = "AgentGate"
    agent_role: str = "一个有帮助的 AI 助手"

    # —— 子 Agent 委派治理（plan/12 §5.1）——
    # 四道闸。多 agent 是唯一没有天然天花板的成本维度：单 agent 的花费被上下文窗口封顶，
    # 而每个子 agent 都是一个全新窗口，且这一切发生在**一次** HTTP 请求内，租户限流只数到 1。
    subagent_enabled: bool = True
    # 委派深度上限。1 = 只允许父直接派子（默认，最安全）。阶段 7 实测无任何深度检查，
    # 离线探针跑出 6 层嵌套——这是刻意的行为收紧，不是纯修复。
    # 放宽到 2 应等 M2 的 delegates_to 清单白名单落地：那时「谁能派给谁」由部署方声明。
    subagent_max_depth: int = 1
    # 单次运行的派发总次数上限（跨轮累计，含嵌套层）。
    subagent_max_per_run: int = 6
    # 整棵委派树共享的 token 预算。耗尽后子 agent 提前收尾并如实回报 stop_reason。
    subagent_token_budget: int = 200_000
    # 同时在跑的子 agent 数上限。tool_executor.MAX_TOOL_CONCURRENCY 只管一批工具同时跑
    # 几个，管不住每个子 agent 内部各自再发的 LLM 调用。
    subagent_max_concurrency: int = 4

    def fallback_model_list(self) -> list[str]:
        return [m.strip() for m in self.fallback_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试可通过 get_settings.cache_clear() 重置。"""
    return Settings()
