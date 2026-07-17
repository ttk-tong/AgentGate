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

    # —— 存储 ——
    database_url: str = Field(
        default="postgresql+asyncpg://agentgate:agentgate@localhost:5432/agentgate"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

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

    def fallback_model_list(self) -> list[str]:
        return [m.strip() for m in self.fallback_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试可通过 get_settings.cache_clear() 重置。"""
    return Settings()
