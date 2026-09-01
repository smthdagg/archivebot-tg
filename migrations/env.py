"""Alembic 迁移环境：target_metadata 指向应用 SQLAlchemy 模型。

数据库 URL 优先取 DATABASE_URL 环境变量，与 app.config.Settings 保持一致；
生产部署用 `alembic upgrade head` 初始化/升级 schema，
开发与测试仍可用 app.database.database.init_db() 的 create_all。
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证能 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.database import models  # noqa: E402,F401  # 触发表注册
from app.database.database import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def _db_url_override() -> None:
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        config.set_main_option("sqlalchemy.url", env_url)


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    _db_url_override()
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直接连接数据库执行迁移。SQLite 使用 batch 模式支持改列。"""
    _db_url_override()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
