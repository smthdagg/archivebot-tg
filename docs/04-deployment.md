# ArchiveBOT-TG 生产部署手册（Doc 04 / M9）

> 目标读者：运维 / 部署人员。按本文档可在全新 VPS 上 15 分钟内跑通 MVP。
> 覆盖：Docker Compose 生产化（.env 全量变量 · 命名卷 · 备份）、PostgreSQL 切换、SQLite 备份/恢复、日志查看与排查、SSRF_ALLOWED_CIDRS 代理环境说明、安全清单。
> 说明书为**可直接复制执行**的命令序列。全文命令假设已在项目根目录 `/opt/archivebot`（示例）下执行。

---

## 0. 拓扑与进程模型

`docker compose` 编排 5 类进程：

| 服务 | 镜像/用途 | 入口命令 |
|---|---|---|
| `bot` | Telegram 长轮询（aiogram 3.x） | `python -m app.bot.main` |
| `api` | Web Admin（FastAPI，默认 `:8080`，含 `GET /healthz`） | `python -m app.main` |
| `worker` | rq 队列消费者（抓取/PDF/上传） | `python -m app.tasks.worker` |
| `redis` | `redis:8-alpine`（rq 队列） | 官方镜像 |
| `postgres` | `postgres:16-alpine`（**仅 PostgreSQL 部署**，SQLite 默认方案不用） | 官方镜像 |

- 数据：`/storage`（临时文件池，任务产物 markdown/pdf/图片/封面）+ 数据库（SQLite 单文件 / PostgreSQL）。
- 队列 `redis` 只存运行中任务，**无持久业务数据**；重启/清空不影响历史与文件（历史依赖 `telegram_file_id` + DB）。
- 依赖：本项目自带 `psycopg[binary]>=3.1`，切 PostgreSQL **无需新增依赖**。

> 仓库根 `docker-compose.yml` 是**开发取向**：`bot/api/worker` 使用 `./app`、`./vendor/ArchiveBOT`、`./data`、`./storage` **bind mount**（宿主机目录覆盖镜像内文件，方便热改）。生产建议用本文档第 4 节的 `docker-compose.prod.yml`（命名卷 + PostgreSQL + 健康检查 + 重启策略）。

---

## 1. 前置要求

- Docker Engine ≥ 24 与 docker compose v2 插件。
- 一台公网 VPS（建议 ≥ 2 vCPU / 4GB RAM / 20GB 磁盘）。Bot 长轮询**不需要公网 443 入站**；Web Admin 若需外网访问才需要 80/443 反代。
- `git`（拉取含 `vendor/ArchiveBOT` 子模块）。
- SSH 密钥对，用于登录 VPS。

---

## 2. 目录与数据卷布局（生产）

```
/opt/archivebot/            # 部署目录（.env、compose 文件、备份脚本）
├── docker-compose.yml      # 官方开发 compose（随仓库）
├── docker-compose.prod.yml # 生产 override/独立 compose（见 §4）
├── .env                    # 全部密钥与配置（绝不入库）
├── backup/                 # 数据库与 storage 备份输出（见 §8/§11）
└── logs/                   # 可选：app 日志落盘（默认打 stdout，见 §7）
```

三块必须持久化的数据：

| 数据 | SQLite 方案 | PostgreSQL 方案 |
|---|---|---|
| 应用数据库 | `data/archivebot.db` 单文件卷 | `postgres-data` 命名卷 |
| 临时文件池 | `/storage` 命名卷 | `/storage` 命名卷 |
| 队列 | `redis-data` 命名卷（可丢） | 同上（可丢） |

---

## 3. 环境变量全量表（`.env`）

从 `.env.example` 复制后按表填写。**除默认值本身，生产必须显式设置：`TELEGRAM_BOT_TOKEN`、`ADMIN_IDS`、`WEB_ADMIN_SECRET`、`WEB_ADMIN_PASSWORD`**。

| 变量 | 类型/默认 | 说明 |
|---|---|---|
| **Telegram** | | |
| `TELEGRAM_BOT_TOKEN` | 必填，空串默认 | @BotFather 获取。bot 进程启动即断言非空，为空直接退出。 |
| `ADMIN_IDS` | JSON 数组，默认 `[]` | 引导为首个 `SUPER_ADMIN` 的 Telegram 数字 ID。**必须是 JSON 数组格式 `ADMIN_IDS=[123456789,987654321]`，不是逗号分隔**（pydantic-settings 对 `list[int]` 用 JSON 解析；`123,456` 会解析失败）。⚠️ 仓库 `.env.example`/README 里的「Comma-separated」写法是误导，按本表用方括号。 |
| `DEFAULT_LANGUAGE` | `auto` | `auto`（按 Telegram `language_code`）\| `zh-CN` \| `en-US`。非法值启动报错。 |
| **数据库 / 队列** | | |
| `DATABASE_URL` | `sqlite:///data/archivebot.db` | SQLite: `sqlite:///文件绝对路径`（容器内用绝对路径时开头四个斜杠，如 `sqlite:////app/data/archivebot.db`）。PostgreSQL: `postgresql+psycopg://用户:密码@host:5432/库名`。**注：int 服务 `DATABASE_URL` 会被 compose `environment:` 覆盖，见 §4 说明。** |
| `REDIS_URL` | `redis://redis:6379/0` | rq 连接串；compose 统一覆盖为 `redis://redis:6379/0`。 |
| **存储（临时文件池）** | | |
| `STORAGE_DIR` | `/storage` | 任务产物目录（`tasks/<uuid>/{metadata.json,article.md,article.pdf,cover.jpg,images/}`）。与 compose `STORAGE_DIR` 保持一致。 |
| `STORAGE_SOFT_LIMIT_MB` | `800` | 达到软限触后台清理。 |
| `STORAGE_HARD_LIMIT_MB` | `1024` | 硬限：拒绝新大任务、立即清理、恢复到清理目标后恢复。 |
| `STORAGE_CLEANUP_TARGET_MB` | `200` | 清理目标水位。 |
| **限制 / 并发** | | |
| `MAX_FILE_SIZE_MB` | `200` | 单文件最大（入队预检）。 |
| `MAX_TASK_SIZE_MB` | `300` | 单任务最大总产出。 |
| `TELEGRAM_MAX_FILE_MB` | `50` | **Bot API `sendDocument` 上限 50MB**，超出跳过上传避免 worker 裸失败。 |
| `MAX_USER_CONCURRENCY` | `2` | 单用户并发任务数。 |
| `MAX_GLOBAL_CONCURRENCY` | `4` | 全局并发任务数（worker 侧 rq 消费配额。多 worker 时按需拆 rq worker 数）。 |
| `TASK_TIMEOUT_SECONDS` | `600` | 任务超时。 |
| `RETRY_COUNT` | `2` | 失败重试次数（**当前配置位存在，重试消费逻辑未实现，见 docs/01 M7 遗留**）。 |
| **功能开关** | | |
| `PDF_ENABLED` | `true` | 是否生成 PDF（需容器内 Chromium）。 |
| `MARKDOWN_ENABLED` | `true` | 是否生成 Markdown。 |
| `IMAGE_ENABLED` | `true` | 是否打包图片 zip / 封面。 |
| `AI_SUMMARY_ENABLED` | `false` | 预留位（Phase 2）；**默认 false，摘要始终用非 LLM 三行正文取句**，避免幻觉。 |
| **Web Admin** | | |
| `WEB_ADMIN_HOST` | `0.0.0.0` | Uvicorn 绑定。生产建议仅监听容器内、由反代 TLS 转发（见 §10），不要直接暴露公网裸 HTTP。 |
| `WEB_ADMIN_PORT` | `8080` | api 端口；compose 映射 `${WEB_ADMIN_PORT:-8080}:8080`。 |
| `WEB_ADMIN_SECRET` | 生产必改 | 签名 admin session cookie 的密钥（`itsdangerous.URLSafeTimedSerializer`）。用 32+ 随机字节。 |
| `WEB_ADMIN_PASSWORD` | 生产必改 | Web Admin 单账号登录密码（MVP；RBAC 身份来自 `ADMIN_IDS`）。用强密码。 |
| **SSRF** | | |
| `SSRF_ALLOWED_CIDRS` | `198.18.0.0/15` | 豁免可抓取的 CIDR 段（逗号分隔）。见 §9 代理环境说明。 |

> ⚠️ 环境变量一律**小写键固定**（大小写不敏感）。每项样例见仓库 `.env.example`。

---

## 4. 生产 compose（命名卷 + PostgreSQL + 健康检查）

生产不直接使用仓库根 `docker-compose.yml` 的 bind mount 方案。将下面内容保存为 `docker-compose.prod.yml`（路径相对 `/opt/archivebot`），并在 `.env` 中加上 PostgreSQL 三项：

```bash
# .env 追加
POSTGRES_USER=archivebot
POSTGRES_PASSWORD=ChangeM3-用URL安全字符（字母+数字+短横/下划线，勿含 : @ / #）
POSTGRES_DB=archivebot
```

```yaml
# docker-compose.prod.yml
# 生产：命名卷 + PostgreSQL + 健康检查 + restart 策略。
# 用法见 §5/§6。存储卷 storage-data 承载 /storage 临时文件池。
services:
  redis:
    image: redis:8-alpine
    restart: unless-stopped
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-archivebot}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env}
      POSTGRES_DB: ${POSTGRES_DB:-archivebot}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-archivebot} -d ${POSTGRES_DB:-archivebot}"]
      interval: 10s
      timeout: 5s
      retries: 10

  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-archivebot}:${POSTGRES_PASSWORD:?}@postgres:5432/${POSTGRES_DB:-archivebot}
      STORAGE_DIR: /storage
      REDIS_URL: redis://redis:6379/0
    command: ["python", "-m", "app.bot.main"]
    volumes:
      - storage-data:/storage
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy

  api:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-archivebot}:${POSTGRES_PASSWORD:?}@postgres:5432/${POSTGRES_DB:-archivebot}
      STORAGE_DIR: /storage
      REDIS_URL: redis://redis:6379/0
    command: ["python", "-m", "app.main"]
    ports:
      - "${WEB_ADMIN_PORT:-8080}:8080"
    volumes:
      - storage-data:/storage
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz',timeout=3)"]
      interval: 15s
      timeout: 5s
      retries: 5

  worker:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-archivebot}:${POSTGRES_PASSWORD:?}@postgres:5432/${POSTGRES_DB:-archivebot}
      STORAGE_DIR: /storage
      REDIS_URL: redis://redis:6379/0
    command: ["python", "-m", "app.tasks.worker"]
    volumes:
      - storage-data:/storage
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
      api:
        condition: service_healthy

volumes:
  redis-data:
  postgres-data:
  storage-data:
```

要点：

- 三服务 `DATABASE_URL` **由 compose `environment:` 覆盖 .env** —— `.env` 里的 `DATABASE_URL` 在这些服务中不生效。改数据库走这里（或改 override）。
- 生产用 `storage-data` / `postgres-data` 命名卷；不再 bind mount `./app`、`./vendor/ArchiveBOT`（镜像内代码为标准）。
- `worker` `depends_on api healthy` 确保 schema 已就绪后再消费（api 每次启动 `init_db()` 幂等建表）。
- `image: .` 的 `Dockerfile` 未设 `ENTRYPOINT`，`CMD` 是默认命令；`run --rm api sh -c "..."` 可覆盖执行一次性命令（如 Alembic）。

---

## 5. 首次部署（复制执行）

在全新 VPS 上，一次性流程：

```bash
# 1) 克隆（含 ArchiveBOT 子模块）
sudo mkdir -p /opt/archivebot && sudo chown "$USER" /opt/archivebot
cd /opt/archivebot
git clone --recurse-submodules <你的仓库URL> .
# 若已克隆缺子模块： git submodule update --init --recursive

# 2) 配置 .env
cp .env.example .env
vim .env
#   必改：TELEGRAM_BOT_TOKEN、ADMIN_IDS(JSON数组[123,...])、WEB_ADMIN_SECRET、WEB_ADMIN_PASSWORD
#   PostgreSQL：追加 POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB，并确认 §4 的 prod override 已保存
chmod 600 .env

# 3) 先启 infra（postgres + redis），等健康
docker compose -f docker-compose.prod.yml up -d postgres redis
docker compose -f docker-compose.prod.yml ps

# 4) 初始化 schema（Alembic 迁移到 head）
docker compose -f docker-compose.prod.yml run --rm --no-deps api sh -c "alembic upgrade head"

# 5) 启动全部服务
docker compose -f docker-compose.prod.yml up -d

# 6) 验证
curl -s http://localhost:8080/healthz          # 期望: {"ok": true}
docker compose -f docker-compose.prod.yml ps   # 全部 running，api healthy
docker compose -f docker-compose.prod.yml logs -f bot  # 期望 bot polling...
docker compose -f docker-compose.prod.yml exec worker python -c "from rq.job import Job; print('ok')"
```

验收：`alembic upgrade head` 无报错、三个应用容器 running、`/healthz` 返回 `{"ok":true}`。

> **SQLite（小规模/单机）**：不想要 PostgreSQL 时，删掉 `postgres` 服务并把三服务的 `DATABASE_URL` 改为 `sqlite:////app/data/archivebot.db`，额外挂 `data:/app/data` 命名卷；其余步骤相同。

---

## 6. PostgreSQL 切换步骤

SQLite 是默认 MVP；生产切 PostgreSQL 只需：DSN + 建库 + Alembic 初始化。**本项目依赖已含 `psycopg[binary]>=3.1`，无需新增安装。**

```bash
# 1) 建库（本地 psql 或容器内）
docker compose -f docker-compose.prod.yml exec postgres sh -c "psql -U \${POSTGRES_USER} -d \${POSTGRES_DB} -c 'select 1'"

# 2) DATABASE_URL 已由 prod override 覆盖为 postgresql+psycopg://archivebot:口令@postgres:5432/archivebot

# 3) Alembic 初始化（幂等，可重复执行）
docker compose -f docker-compose.prod.yml run --rm --no-deps api sh -c "alembic upgrade head"

# 4) 模型变更后生成新迁移（本地开发态，先停服务避免与生产冲突）
DATABASE_URL=postgresql+psycopg://archivebot:口令@localhost:5432/archivebot alembic revision --autogenerate -m "描述"
DATABASE_URL=postgresql+psycopg://archivebot:口令@localhost:5432/archivebot alembic upgrade head   # 本地验证
```

`alembic upgrade head` 从 `DATABASE_URL` 读连接（`migrations/env.py` 优先取 os 环境变量）。应用进程启动时另有 `init_db()`（`create_all`）兜底，两者幂等共存，以 Alembic 为权威 schema 来源。

> **从已有 SQLite 迁移数据到 PostgreSQL 无内置工具**：SQLAlchemy 模型含外键，逐表 `INSERT ... SELECT` 需自行处理依赖顺序。MVP 建议新库从零开始（历史 `telegram_file_id` 取决于 Telegram 侧，换库即视为重启）。若必须搬数据，按依赖序导出：`users → user_applications → tasks → files → audit_logs → system_settings`，并先跑 `alembic upgrade head` 建表再灌数据。

---

## 7. 日志查看与排查

应用统一 `logging.basicConfig(level=INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")` 打到 **stdout**，容器 stdout 交给 Docker。无内置文件轮转；落盘由 Docker 日志驱动（`json-file`/`local`）控制。

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=200 bot      # 实时跟 bot
docker compose -f docker-compose.prod.yml logs --since=10m api        # 近 10 分钟 api
docker compose -f docker-compose.prod.yml logs --tail=100 worker | grep -iE "error|fail|traceback"
docker compose -f docker-compose.prod.yml ps                          # 状态/退出码/健康
```

常见问题速查：

| 现象 | 原因/处置 |
|---|---|
| `bot` 反复退出，日志 `TELEGRAM_BOT_TOKEN is not set` | `.env` 未填 token，或 env_file 未生效。检查 `.env` 行格式、无引号包裹。 |
| bot/api/worker 起不来，`connection ... refused`/`Is the server running` | PostgreSQL 未健康。先 `up -d postgres redis` 并等 healthcheck 绿，再启应用。 |
| 日志大量 `ssrf ... denied` | URL 命中 SSRF 拦截（内网/环回/云 metadata），或代理 fake-IP 段被误拦——见 §9。 |
| worker 跑起来但任务一直 `QUEUED` | redis 不可达或队列名不匹配；看 worker `worker starting, queue=...` 是否出现。 |
| 任务 `FAILED` 上传阶段 | 产物超 `TELEGRAM_MAX_FILE_MB`（默认 50）被跳过上传；或 Telegram 侧限流。看 worker traceback。 |
| 队列堆积 Redis 内存上涨 | 任务全部失败进 rq failed 队列。用 `redis-cli llen rq:queue:*` 排查，确认无泄漏 token。 |
| `/healthz` 通但页面 401/403 | 未登录或 session 过期（24h）；需 `WEB_ADMIN_SECRET` 稳定（改动后所有会话失效重登）。 |

---

## 8. SQLite 备份与恢复

默认 SQLite 部署（单文件 `data/archivebot.db`）。**最简单可靠的是停机冷备**（免锁、免一致性风险）：

```bash
# 冷备：停机 → 拷贝 → 启动（*nix 定时任务建议 0 3 * * *，保留最近 7 份）
docker compose -f docker-compose.prod.yml stop api worker bot
mkdir -p backup
cp data/archivebot.db "backup/archivebot-$(date +%F).db"
docker compose -f docker-compose.prod.yml start
```

**在线热备**（不停机）用 sqlite3 一致性 `backup` API，不要裸 `cp` 一个写入中的库文件。在 api 容器内执行：

```bash
docker compose -f docker-compose.prod.yml exec -T api python -c '
import sqlite3
src = sqlite3.connect("/app/data/archivebot.db")
dst = sqlite3.connect("/tmp/archivebot-live.db")
src.backup(dst); dst.close(); src.close()
'
docker compose -f docker-compose.prod.yml cp "api:/tmp/archivebot-live.db" "backup/archivebot-$(date +%F).db"
```

恢复（停机替换）：

```bash
docker compose -f docker-compose.prod.yml stop api worker bot
cp backup/archivebot-YYYY-MM-DD.db data/archivebot.db
chmod 600 data/archivebot.db
docker compose -f docker-compose.prod.yml start
```

> 冷备最简单且免锁：停机 → `cp data/archivebot.db backup/` → 启动。定时任务建议 `0 3 * * *` 冷备 + 保留最近 7 份。

---

## 9. SSRF_ALLOWED_CIDRS 与代理环境

`app/archive/ssrf.py`：抓取前 URL 主机 DNS 解析后拒绝私有/环回/链路本地/云 metadata（`169.254.169.254`、`metadata.google.internal`、阿里云 `100.100.100.200` 等）；`requests` 层 `ssrf_guard` 另可覆盖重定向每一跳。

`SSRF_ALLOWED_CIDRS`（默认 `198.18.0.0/15`）声明**豁免段**：命中该段的 IP 视为可抓取，不按私有地址拦截。

**典型代理环境**：Clash / sing-box / Clash Verge 等**透明代理 / fake-IP** 会把所有域名解析到 `198.18.0.0/15` 这一**不可公网路由**的保留段后再转发。若按普通私有地址一律拦截（`is_private`/`is_loopback` 等），解析结果落在该段就会让**全部抓取失败**。默认豁免正是为此。该段在公网不可路由，豁免不引入真实内网风险。

| 场景 | 配置建议 |
|---|---|
| VPS 无透明代理，直连公网 | 保留默认 `198.18.0.0/15` 无害（段不可路由，永不会命中真实目标）。 |
| VPS 走 Clash/sing-box fake-IP 透明代理 | **必须保留 `198.18.0.0/15`**，否则全部域名被误拦截。 |
| egress 需访问内网服务（自建私有网关抓取） | 把对应段加进 `SSRF_ALLOWED_CIDRS`（如 `SSRF_ALLOWED_CIDRS=198.18.0.0/15,10.0.0.0/8`）。⚠️ **这是主动收窄安全边界**：等于允许 worker 抓取该网段任意内网主机，只应在信任的内网抓取网关上临时放宽，且尽量收窄到具体子网。 |
| 想收紧（无代理） | 可清空 `SSRF_ALLOWED_CIDRS=`（空串→不豁免任何段）。 |

> 残留风险（docs/02 §7）：`curl_cffi`（知乎）与 Playwright 渲染不走 `requests` 守卫；DNS rebinding 需 egress 网络策略兜底。生产把容器网络限定在 VPS，不开放任意 egress 到完全内网段。

---

## 10. 安全清单（上线前逐项核对)

- [ ] **Token 保护**：`TELEGRAM_BOT_TOKEN`、`WEB_ADMIN_PASSWORD`、`WEB_ADMIN_SECRET`、`POSTGRES_PASSWORD` 只在 `.env`（`chmod 600`），`.env` 已在 `.gitignore`/`.dockerignore` 排除，永不入库；泄露即轮换。
- [ ] **Web Admin 强认证**：`WEB_ADMIN_SECRET` 用 ≥32 字节随机串；`WEB_ADMIN_PASSWORD` 用强密码；改 `SECRET` 会让旧会话失效，属预期。
- [ ] **HTTPS 反代**：Web Admin 是轻量 MVP（无内置 TLS、**无 CSRF 防护，已知待办见 docs/01 M6/M7**）。生产必须置于 Caddy/Traefik/nginx 反向代理后强制 TLS，并用 DNS/防火墙把 `8080` 限制为仅反代来源；不要把裸 HTTP 端口暴露到公网。示例 Caddy：`archivebot.example.com { reverse_proxy 127.0.0.1:8080 }`。
- [ ] `ADMIN_IDS` 用 JSON 数组（`[123,...]`），确保首管理员能提升为 SUPER_ADMIN。
- [ ] **端口收敛**：只用反代暴露 `8080`；`5432`、`6379` 仅在 compose 内网；不要让 postgres/redis 监听公网。
- [ ] **SSRF 收敛**：确认 §9 的 `SSRF_ALLOWED_CIDRS` 未无谓放宽（默认只含不可路由的 fake-IP 段）。
- [ ] 防火墙（ufw/云 SG）：仅开放 22 与（如需）80/443。
- [ ] 备份可恢复：按 §8/§11 做过一次性「备份→恢复演练」，不只是有备份文件。
- [ ] `vendor/ArchiveBOT` 只读，不改动上游；升级它走子模块 pin + 回归（pytest）。

---

## 11. 备份与升级策略

**备份（PostgreSQL 方案）**

```bash
# DB（pg_dump 一致性）
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-archivebot}" -d "${POSTGRES_DB:-archivebot}" -Fc -f - > backup/archivebot-$(date +%F).dump

# 存储（临时文件池；历史不依赖它，但便于追溯本轮产物）
# tar 快照 storage-data 卷（生产建议 rsync 到异地）
docker run --rm -v archivebot_storage-data:/s -v "$PWD/backup":/b alpine tar czf /b/storage-$(date +%F).tgz -C /s .
```

恢复：`pg_restore --clean` 到新库，再启动应用。

**SQLite 方案**：见 §8（在线 `sqlite3 backup()` 或停机冷备）。

**升级**：

```bash
cd /opt/archivebot
git pull --recurse-submodules && git submodule update --init --recursive
docker compose -f docker-compose.prod.yml build
# 先跑迁移再拉起应用
docker compose -f docker-compose.prod.yml stop bot api worker
docker compose -f docker-compose.prod.yml run --rm --no-deps api sh -c "alembic upgrade head"
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps   # 确认全绿
```

大版本/含模型变更的升级：先备份（§8/§11），再 `alembic upgrade head`，最后停旧起新。

---

## 12. 相关文档

- 设计拆分与**进度状态表**：[docs/01-task-breakdown.md](01-task-breakdown.md)（M9 状态，含后端 PostgreSQL 生产化 Phase 2 备注）
- 架构决策（ADR-5 DSN 可切 PostgreSQL、ADR-9 SSRF、ADR-10 容器化）：[docs/02-architecture.md](02-architecture.md)
- 验收清单：[docs/03-acceptance.md](03-acceptance.md)
- 开发/协作契约：[AGENTS.md](../AGENTS.md)；快速开始：[README.md](../README.md)