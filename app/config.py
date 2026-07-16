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


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试可通过 get_settings.cache_clear() 重置。"""
    return Settings()
