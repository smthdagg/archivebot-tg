"""应用配置：环境变量加载与校验（对应设计规格 §45）。"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
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
    # Bot API sendDocument 上限 50MB，超出直接跳过上传（避免 worker 裸失败）
    telegram_max_file_mb: int = 50
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
    # ---- Web Admin 限流（规格 §50，M7）----
    web_admin_rate_limit: int = 100  # /admin 全局每分钟每 IP 请求上限
    web_admin_rate_window_seconds: int = 60
    web_admin_login_max_failures: int = 5  # 窗口内连续失败次数
    web_admin_login_window_seconds: int = 900
    web_admin_login_lockout_seconds: int = 900  # 触发锁定后的临时锁定秒数（指数退避基数）

    # ---- SSRF ----
    # 豁免的 CIDR 段（逗号分隔）。默认豁免 198.18.0.0/15：Clash/sing-box 等
    # 透明代理的 fake-IP DNS 会把所有域名解析到该段，按私有地址拦截会使
    # 代理环境下全部抓取失效。该段不可在公网路由，豁免不引入内网风险。
    ssrf_allowed_cidrs: str = "198.18.0.0/15"

    # ---- Cookie Profile（Phase 2，登录类网站）----
    # 用户自备 cookie，仅在用户自己登录过的网站使用（规格红线，不用于绕过付费墙）。
    # 结构：profile 名 → { 平台 → [Cookie-Editor 格式 cookie 字典] }。
    # 加载来源：COOKIE_PROFILES（JSON 字符串）与/或 COOKIE_PROFILES_FILE（JSON 文件）。
    cookie_profiles: dict[str, dict[str, list[dict[str, Any]]]] = Field(default_factory=dict)
    cookie_profiles_file: str = ""

    @model_validator(mode="after")
    def _load_cookie_profiles_file(self) -> "Settings":
        if self.cookie_profiles_file:
            path = Path(self.cookie_profiles_file)
            if not path.exists():
                raise ValueError(f"COOKIE_PROFILES_FILE not found: {path}")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"COOKIE_PROFILES_FILE invalid JSON: {e}") from e
            if not isinstance(data, dict):
                raise ValueError("COOKIE_PROFILES_FILE must be a JSON object (profile → platforms)")
            merged = dict(self.cookie_profiles)
            merged.update(data)
            object.__setattr__(self, "cookie_profiles", merged)
        return self

    @field_validator("cookie_profiles")
    @classmethod
    def _validate_cookie_profiles(cls, v: dict) -> dict:
        for name, platforms in v.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("cookie profile name must be a non-empty string")
            if not isinstance(platforms, dict):
                raise ValueError(
                    f"cookie profile {name!r} must map platform → list of cookies"
                )
            for platform, cookies in platforms.items():
                if not isinstance(cookies, list):
                    raise ValueError(
                        f"cookie profile {name!r} platform {platform!r} must be a list"
                    )
        return v

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
