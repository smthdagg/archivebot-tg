"""应用配置：环境变量加载与校验（对应设计规格 §45）。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Telegram ----
    telegram_bot_token: str = Field(default="", description="BotFather 提供的 token")
    admin_ids: list[int] = Field(default_factory=list, description="引导为 SUPER_ADMIN 的 Telegram ID")
    default_language: str = "auto"  # zh-CN | en-US | auto

    # ---- Database / Queue ----
    database_url: str = "sqlite:///data/archivebot.db"
    redis_url: str = "redis://redis:6379/0"

    # ---- Storage（临时文件池）----
    storage_dir: Path = Path("/storage")
    storage_soft_limit_mb: int = 800
    storage_hard_limit_mb: int = 1024
    storage_cleanup_target_mb: int = 200

    # ---- 限制与并发 ----
    max_file_size_mb: int = 200
    max_task_size_mb: int = 300
    max_user_concurrency: int = 2
    max_global_concurrency: int = 4
    task_timeout_seconds: int = 600
    retry_count: int = 2

    # ---- 功能开关 ----
    pdf_enabled: bool = True
    markdown_enabled: bool = True
    image_enabled: bool = True
    ai_summary_enabled: bool = False

    # ---- Web Admin ----
    web_admin_host: str = "0.0.0.0"
    web_admin_port: int = 8080
    web_admin_secret: str = "change-me-to-a-long-random-string"
    web_admin_password: str = "change-me"

    @field_validator("default_language")
    @classmethod
    def _validate_language(cls, v: str) -> str:
        if v not in ("zh-CN", "en-US", "auto"):
            raise ValueError("default_language must be zh-CN, en-US or auto")
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def soft_limit_bytes(self) -> int:
        return self.storage_soft_limit_mb * 1024 * 1024

    @property
    def hard_limit_bytes(self) -> int:
        return self.storage_hard_limit_mb * 1024 * 1024

    @property
    def cleanup_target_bytes(self) -> int:
        return self.storage_cleanup_target_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """进程内缓存配置单例。"""
    return Settings()
