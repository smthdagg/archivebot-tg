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

# Install Python deps first for layer caching
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install aiogram fastapi uvicorn jinja2 python-multipart itsdangerous \
                pydantic pydantic-settings sqlalchemy alembic psycopg[binary] \
                redis rq requests curl_cffi playwright tqdm \
                beautifulsoup4 lxml markdownify \
                markdown trafilatura readability-lxml python-dotenv \
                "yt-dlp>=2024.10.22" && \
    playwright install --with-deps chromium

# Project sources (vendor/ArchiveBOT is mounted/checked-out by docker-compose)
COPY app ./app

CMD ["python", "-m", "app.main"]
