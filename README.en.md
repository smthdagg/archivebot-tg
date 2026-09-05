# ArchiveBOT-TG — Telegram Internet Document Archiving & Delivery Platform

[English](README.en.md) | [简体中文](README.md)

A robust multi-user archiving gateway: send a link, receive the document.

**Core flow**: send any article/webpage URL in a Telegram private chat → platform auto-detected → pick `PDF / Markdown / Full-page screenshot / All` → queued (cancellable) → receive "title + 3-line excerpt + original link + file" → history is searchable, re-sendable, re-archivable and deletable; local files are cleaned up automatically after upload.

> Built on top of [smthdagg/ArchiveBOT](https://github.com/smthdagg/ArchiveBOT) (git submodule `vendor/ArchiveBOT`) for platform scraping — the Telegram gateway does not re-implement scraping logic. Milestones & **progress table**: [docs/01-task-breakdown.md](docs/01-task-breakdown.md) · Architecture: [docs/02-architecture.md](docs/02-architecture.md) · Contributing: [AGENTS.md](AGENTS.md).

> Designed for personal/team knowledge archiving and offline reading. Respects site access controls and paywalls — only content **you** can legitimately access is archived.

## Features

- **Bot experience**: bilingual (`en-US` / `zh-CN`), approval flow, URL auto-detection & preview, format selection, live task status with cancel, completion message with a verbatim 3-line excerpt (no LLM, zero hallucination)
- **Platform coverage**
  - Text: WeChat Official Accounts, `x.com/twitter`, Xiaohongshu (RED), Weibo, Zhihu, Reddit, generic web (Playwright + Readability)
  - Video: YouTube / Bilibili / Douyin / Kuaishou / Instagram (yt-dlp, single `video.mp4` delivery); TikTok not yet adapted
  - **Caixin paywall specialization**: stealth login injection, **multi-page article stitching (`?p1..?pN`)**, lead-image recovery with cross-page dedup, prompt-injection honeypot removal, `/m/` mobile-link normalization
- **Full-page screenshot**: same template as PDF, `full_page` PNG (800px wide, 2x, auto-JPEG over 40MB)
- **History & search**: pagination, keyword/platform search, details, **re-send via `telegram_file_id`** (no re-scrape), **re-archive** (new version), delete
- **Stealth rendering**: login-required platforms (Caixin/WeChat) run on **Patchright** (anti-detection Playwright fork that strips automation fingerprints); render sessions **write back updated cookies** to the profile (follows server-side token rotation, same as your browser); expired cookies fail fast with `LOGIN_REQUIRED` instead of silently delivering truncated content
- **Cookie Profiles (login-required sites)**: configure via `COOKIE_PROFILES` / `COOKIE_PROFILES_FILE` for `wechat / xhs / reddit / zhihu / twitter` (`auth_token+ct0`); injected per-task, **only** for sites you yourself logged into — never bypassing access control
- **Task self-healing**: stale tasks from worker crashes/migrations are automatically reaped (`reap_stale_tasks`); concurrency-limit errors are friendly (`CONCURRENCY_LIMIT` — "try again later")
- **Web Admin**: FastAPI + Jinja2 at `http://localhost:8080/admin` — dashboard, users, tasks, logs, session login, rate limiting & lockout
- **Storage**: task dirs `tasks/<uuid>/…`; soft 800MB / hard 1GB / target 200MB watermarks; local files are deleted after successful upload (history relies on `file_id` only)
- **Security**: layered SSRF protection (per-redirect guard), server-side callback ownership checks, RBAC (`ADMIN_IDS` → SUPER_ADMIN), concurrency & size limits
- **Delivery quality**: artifacts named `Title_YYYY-MM-DD_HHMM.ext`; Markdown delivery references remote images (single-file viewable); PDF/screenshot layout — title & byline centered, info block (author/source/published/link) at the end

## Requirements

- Docker (with `docker compose`)
- Python 3.12 for local development (container: `python:3.12-slim` + Playwright/Patchright Chromium + CJK fonts + `ffmpeg`)
- A Telegram bot token from `@BotFather`

## Quick Start (Docker — recommended)

```bash
cp .env.example .env    # fill TELEGRAM_BOT_TOKEN, ADMIN_IDS, etc.
docker compose up --build
```

| Service | Purpose |
|---|---|
| `bot` | Telegram long polling (aiogram 3.x) |
| `api` | Web Admin `http://localhost:8080/admin` |
| `worker` | rq queue consumer (scrape → generate → upload) |
| `redis` | queue broker (`redis:8-alpine`) |

The first `ADMIN_IDS` entry bootstraps as SUPER_ADMIN.

## Local Development

```bash
git clone --recurse-submodules https://github.com/smthdagg/archivebot-tg.git
cd archivebot-tg
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium

.venv/bin/python -m pytest -q                # full suite (no Redis/Telegram needed)
.venv/bin/python -m pytest -m "not slow" -q  # skip the slow anti-bot probe cases
.venv/bin/ruff check app/ tests/ scripts/ migrations/

# End-to-end self-check (real scrape → MD → PDF → screenshot; prints E2E-OK)
STORAGE_DIR=/tmp/ab-storage DATABASE_URL=sqlite:////tmp/ab.db \
  .venv/bin/python scripts/e2e_archive.py "https://en.wikipedia.org/wiki/Markdown"
```

### Database migrations (Alembic)

```bash
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic upgrade head
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic revision --autogenerate -m "desc"
```

CI runs `ruff + pytest + alembic upgrade head` on every push/PR.

## Usage

### Telegram

- Send a link (`x.com/.../status/...`, `mp.weixin.qq.com/s/...`, weekly.caixin.com, …) → the bot previews platform & title
- Pick `📄 PDF / 📝 Markdown / 🖼 Screenshot / 📦 All`; processing can be cancelled
- On completion you receive the file(s) with a verbatim excerpt; history supports re-send and re-archive
- Login-required tweets (X) and paywalled Caixin articles require a **Cookie Profile** (see below) — otherwise the task fails fast with a readable `Login required.` error

### Cookie Profiles (login-required sites)

Only for content you can legitimately access with **your own** login; never auto-attached:

- Sources: `COOKIE_PROFILES` (inline JSON) and/or `COOKIE_PROFILES_FILE` (JSON file), merged (file wins)
- Format (Cookie-Editor style): `{"<profile>": {"<platform>": [{"name","value","domain","path"}]}}`
- Wired platforms: `wechat / reddit` (file-based), `zhihu / twitter` (`auth_token+ct0`) / `xhs` (method-based); `web / weibo` ignore profiles
- Caixin sessions **auto-follow token rotation** (cookies are written back after each render); when they do expire, tasks fail fast with `LOGIN_REQUIRED` until you re-export
- Details & red lines: [docs/05-cookie-profile.md](docs/05-cookie-profile.md)

### Deploy to your own VPS

```bash
# Private connection params in scripts/deploy.env.local (gitignored):
#   VPS_HOST=<your-vps-address>
#   VPS_PORT=<ssh-port>
./scripts/deploy-to-vps.sh                 # gates → package → upload → build → restart → healthcheck
./scripts/deploy-to-vps.sh --skip-build    # code/template-only changes
```

The script excludes `.env` / `data` / `storage` — your server-side config and data are never overwritten.

## Configuration

Key `.env` options (see [.env.example](.env.example)):

```
TELEGRAM_BOT_TOKEN=
ADMIN_IDS=[123456789]
DEFAULT_LANGUAGE=auto
DATABASE_URL=sqlite:///data/archivebot.db
REDIS_URL=redis://redis:6379/0
STORAGE_DIR=/storage
COOKIE_PROFILES=
COOKIE_PROFILES_FILE=data/cookie_profiles.json
WEB_ADMIN_HOST=0.0.0.0
WEB_ADMIN_PORT=8080
WEB_ADMIN_SECRET=change-me-to-a-long-random-string
WEB_ADMIN_PASSWORD=change-me-strong-password
```

## Documentation

- [AGENTS.md](AGENTS.md) — module boundaries, hard rules (vendor read-only, SSRF, i18n pairs, callback ownership)
- [docs/01-task-breakdown.md](docs/01-task-breakdown.md) — milestones M0–M9 progress table (source of truth)
- [docs/02-architecture.md](docs/02-architecture.md) — architecture, ADRs, data model, flows, security
- [docs/04-deployment.md](docs/04-deployment.md) / [docs/05-cookie-profile.md](docs/05-cookie-profile.md)

## Version

### 1.0.1 (latest)

- **Stealth rendering**: Caixin/WeChat switched to Patchright; Caixin session write-back solves repeated session invalidation
- **X delivery**: Tweet → artifact bridge; consistent `LOGIN_REQUIRED` error codes
- **Self-healing**: stale-task reaping + friendly `CONCURRENCY_LIMIT` message
- **Layout**: centered title & byline for PDF/screenshot; info block moved to the end
- **Ops**: deploy contract (`.env` never overwritten), daily backups, health monitoring, log rotation, cleanup de-conflicted with business data

### 1.0.0

First public release: full-text archiving (PDF / Markdown / screenshot) + queued delivery + history & Web Admin + SSRF/RBAC/limits.

Known limitations: TikTok not adapted; some X content requires your own login cookie profile.

## Directory Structure

```
app/
  bot/        # Telegram layer: handlers / keyboards / i18n / middleware / delivery
  archive/    # core: detector / runner / cleaner / markdown / pdf / screenshot / images / excerpt / fetcher / ssrf(+guard) / cookie_profile / stealth
  tasks/      # queue (rq) / worker / manager / jobs
  storage/    # temp file pool & cleanup watermarks
  database/   # SQLAlchemy models / enums / services
  admin/      # Web Admin (FastAPI + Jinja2)
  config.py   # pydantic-settings

vendor/ArchiveBOT/   # git submodule (platform scrapers, read-only)
storage/  data/      # runtime volumes (gitignored)
tests/  migrations/  scripts/  docs/
```

## License

No license granted yet; all rights reserved by default. Add a `LICENSE` file before open-sourcing if desired.
