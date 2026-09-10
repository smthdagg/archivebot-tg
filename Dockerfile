# syntax=docker/dockerfile:1
# 代码使用 StrEnum（3.11+），与本地开发/CI 统一 3.12
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System deps for Playwright/Chromium, lxml, and yt-dlp video merge (ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        fonts-noto-cjk fonts-liberation \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
        ffmpeg \
        && rm -rf /var/lib/apt/lists/*

# Node 22 + weread-omni：公众号订阅经微信读书 API（scripts/weread_bridge.mjs 桥接，
# 上游硬要求 Node >= 22.13）。全局装 npm 包，桥接脚本随代码 COPY。
RUN apt-get update && apt-get install -y --no-install-recommends gnupg \
        && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
        && apt-get install -y --no-install-recommends nodejs \
        && npm install -g --no-audit --no-fund weread-omni@0.1.1 \
        && npm cache clean --force \
        && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install aiogram fastapi uvicorn jinja2 python-multipart itsdangerous \
                patchright \
                pydantic pydantic-settings sqlalchemy alembic psycopg[binary] \
                redis rq requests curl_cffi playwright tqdm \
                beautifulsoup4 lxml markdownify \
                markdown trafilatura readability-lxml python-dotenv \
                httpx qrcode \
                "camoufox[geoip]>=0.5" \
                "yt-dlp>=2024.10.22" && \
    playwright install --with-deps chromium && \
    patchright install chromium

# Project sources (vendor/ArchiveBOT is mounted/checked-out by docker-compose)
COPY app ./app
COPY vendor/wechat_to_md ./vendor/wechat_to_md
COPY scripts ./scripts
# Alembic：容器内可直接 `alembic upgrade head`（docs/07 §8）
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

CMD ["python", "-m", "app.main"]
