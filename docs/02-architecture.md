# ArchiveBOT-TG 技术架构（Architecture）

> 版本：v1.0.0（首个对外发布）
> 依据设计规格 §35/§46/§47/§48/§50 落地；发布分支 `main`，见 `docs/01` 状态表与 README 1.0 说明。

---

## 1. 架构决策记录（ADR 摘要）

| 编号 | 决策 | 理由 |
|---|---|---|
| ADR-1 | 新建独立仓库 `archivebot-tg`，ArchiveBOT 以 **git 子模块** 引入 `vendor/ArchiveBOT` | 规格要求"Telegram Gateway 不重复实现平台抓取逻辑"，复用 ArchiveBOT 的平台适配能力；子模块 pin 版本，便于升级与回溯 |
| ADR-2 | Telegram 层用 **aiogram 3.x**（而非 ArchiveBOT 内置的 python-telegram-bot） | 规格 §46 指定；aiogram 3 为 async 原生、内联键盘/对话状态管理更强，适合多用户复杂交互 |
| ADR-3 | ArchiveBOT 复用方式 = **进程内 import**（worker 直接调用其 `services.*` 提取函数），不另起 HTTP 服务 | ArchiveBOT 的服务层本就设计为进程内调用；省一层网络与鉴权；Flask 单体不作为依赖启动 |
| ADR-4 | API/Web Admin 用 **FastAPI + Jinja2 + HTMX** | 规格 §46；HTMX 保持轻量、无前端构建链；Bot 日常管理仍为主入口 |
| ADR-5 | 数据库 **SQLAlchemy 2.0**，MVP 用 **SQLite**，DSN 可切换 **PostgreSQL** | 规格 §46/§51；模型层与方言解耦，Alembic 迁移在 M9 落地 |
| ADR-6 | 队列 **Redis + rq**（MVP） | 规格 §35 用 Redis Queue；rq 简单可靠、天然支持延迟/重试，满足 MVP 全局并发 4 |
| ADR-7 | PDF 生成 **Chromium Print-to-PDF**（Playwright），HTML 模板渲染；长截图同模板 `full_page` 全页图（`screenshot.py`） | 规格 §11 链路为 Browser Render→Clean→Normalized MD→HTML Template→Chromium PDF/全页图；对中文/复杂排版还原度高于 WeasyPrint；`Markdown` 交付改远程图片引用、产物命名 `标题_日期_时间.ext` |
| ADR-8 | 三行摘要 **不调用 LLM**，从清洗后正文取前三个有效句子 | 规格 §10：避免幻觉、与原文一致、省成本、更快；AI Summary 作为 Phase 2 可选项 |
| ADR-9 | SSRF 防护在**入队前**做 URL 校验，worker 内再次防御 | 规格 §50；双保险（bot 快速失败 + worker 兜底）；`ssrf_guard` 劫持 `Session.send` 覆盖重定向每一跳 |
| ADR-10 | 本地环境以 Docker 为准（`python:3.12-slim`），宿主机仅用于编辑/测试 | ArchiveBOT 依赖 Playwright/Chromium 与系统库，容器化最稳；本地开发用 `uv`/`.venv`（3.12），CI 统一 3.12 |
| ADR-11 | `X` 等登录墙平台**仅用用户自备登录态**（Cookie Profile） | 规格红线：不绕过访问控制；`twitter/xhs/wechat/reddit/zhihu` 已修通注入（`auth_token/ct0` 双域），未授权时规范落 `LOGIN_REQUIRED`；真实抓取需 `cookie_profile`，鉴别单元不碰外网 |

---

## 2. 系统架构

```
                     Telegram (Bot API, long polling)
                              │
              ┌───────────────┴───────────────┐
              │        app.bot (aiogram)      │  用户管理 / 菜单 / 格式选择 / 历史 / 设置
              └───────────────┬───────────────┘
                              │ 创建任务 / 查询 / 上传回调
              ┌───────────────┴───────────────┐
              │        app.api (FastAPI)      │  Web Admin (Jinja2+HTMX) + 内部 API
              └───────────────┬───────────────┘
                              │
              ┌───────────────┴───────────────┐
              │     app.tasks (Task Manager)  │  状态机 / 并发限制 / 取消 / 重试
              └───────────────┬───────────────┘
                              │ Redis Queue (rq)
              ┌───────────────┴───────────────┐
              │     app.tasks.worker          │  消费队列
              │     └─ app.archive.runner     │  编排抓取→清洗→生成
              │        └─ vendor/ArchiveBOT   │  平台适配：wechat/x/twitter/xhs/weibo/
              │           services.*          │  zhihu/reddit/youtube/bilibili/douyin/web
              └───────────────┬───────────────┘
                              │ 产出文件
              ┌───────────────┴───────────────┐
              │    app.storage (/storage)     │  tasks/<uuid>/{metadata.json,article.md,article.pdf,cover.jpg,images/}
              └───────────────┬───────────────┘
                              │ 上传 sendDocument
                    Telegram 文件交付 (file_id 落库)
                              │
              ┌───────────────┴───────────────┐
              │   SQLite/PostgreSQL (app.db)  │  历史 / 用户 / 文件引用 / 审计（与本地文件分离）
              └───────────────────────────────┘
```

### 进程模型（MVP）

| 服务 | 进程 | 说明 |
|---|---|---|
| bot | 1 个 asyncio 进程 | aiogram long polling；同时托管 FastAPI（Uvicorn 同进程 or 独立进程，MVP 独立进程更清晰） |
| api | 1 个 Uvicorn 进程 | Web Admin（Jinja2 + `edit_reply_markup`/`edit_message` 菜单与状态收尾） |
| worker | 1 个 rq worker（可扩） | 阻塞式抓取/生成（`webpage_patch`/`wechat_patch` 含登录态与分页合并）/上传；处理完回调 Telegram |
| redis | 官方镜像 | 队列 + 轻量缓存 |
| db | SQLite 卷（MVP） | 挂载 volume；可切换 PostgreSQL |

---

## 3. 目录结构

```
ArchiveBot/                        # 仓库根（archivebot-tg）
├── app/
│   ├── __init__.py
│   ├── config.py                  # pydantic-settings 配置
│   ├── bot/                       # aiogram 层
│   │   ├── __init__.py
│   │   ├── main.py                # bot 启动/装配
│   │   ├── handlers/
│   │   │   ├── __init__.py
│   │   │   ├── start.py           # /start、注册审批
│   │   │   ├── archive.py         # URL 下载流程、格式选择、状态、完成消息
│   │   │   ├── history.py         # 历史、搜索、详情、重新发送/重新抓取
│   │   │   ├── settings.py        # 设置
│   │   │   └── admin.py           # 管理中心（Bot 侧）
│   │   ├── keyboards/             # 内联键盘构造
│   │   ├── middleware/            # 用户态/语言/限流中间件
│   │   └── i18n/
│   │       ├── __init__.py        # 加载器：get(key, lang)
│   │       └── locales/
│   │           ├── zh-CN/messages.json
│   │           └── en-US/messages.json
│   ├── archive/                   # 归档核心（worker 侧）
│   │   ├── __init__.py
│   │   ├── detector.py            # URL→平台识别
│   │   ├── runner.py              # 任务编排
│   │   ├── cleaner.py             # DOM 清洗
│   │   ├── markdown.py            # 规范化 Markdown
│   │   ├── pdf.py                 # HTML 模板→Chromium PDF
│   │   ├── images.py              # 图片下载/封面
│   │   ├── excerpt.py             # 三行原文摘要
│   │   └── ssrf.py                # URL/主机安全校验
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── queue.py               # rq 封装
│   │   ├── worker.py              # 消费与执行
│   │   └── manager.py             # 任务 CRUD/状态/取消/并发
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── manager.py             # 任务目录与配额
│   │   └── cleanup.py             # 软/硬限清理
│   ├── database/
│   │   ├── __init__.py
│   │   ├── database.py            # engine/session
│   │   ├── models.py              # SQLAlchemy 模型
│   │   └── enums.py               # 常量枚举
│   ├── admin/
│   │   ├── __init__.py
│   │   ├── main.py                # FastAPI 装配
│   │   ├── routes.py              # Web Admin 路由
│   │   ├── auth.py                # Session 登录/RBAC
│   │   └── templates/             # Jinja2 模板
│   └── main.py                    # api 入口（uvicorn）
├── vendor/
│   └── ArchiveBOT/                # git 子模块（pin 上游版本）
├── storage/                       # 运行期临时文件池（volume）
├── data/                          # SQLite 数据（volume，gitignore）
├── tests/
├── docs/
│   ├── 01-task-breakdown.md
│   └── 02-architecture.md
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

---

## 4. 技术栈

| 层 | 选型 | 版本建议 |
|---|---|---|
| 运行时 | Python | 3.12（容器内 `python:3.12-slim`） |
| Telegram | aiogram | 3.x |
| Web/API | FastAPI + Uvicorn | 0.1xx |
| 模板 | Jinja2 + HTMX（CDN） | — |
| ORM | SQLAlchemy | 2.0 |
| DB | SQLite（MVP）→ PostgreSQL | 内置 / 16+ |
| 迁移 | Alembic | 1.13+ |
| 队列 | Redis + rq | 7 / 1.16 |
| 浏览器 | Playwright + Chromium / CJK 字体 / ffmpeg | 1.x（容器内 `fonts-noto-cjk`，`yt-dlp` 视频合并） |
| 内容提取 | ArchiveBOT `services.*`（子模块） | pin commit |
| 正文清洗 | ArchiveBOT 依赖：readability-lxml / trafilatura | 复用其 requirements |
| PDF | Playwright page.pdf()（Chromium） | — |
| 配置 | pydantic-settings | 2.x |
| 日志 | stdlib logging + JSON formatter（可选） | — |
| 测试 | pytest + pytest-asyncio | 8.x |
| Lint | ruff | 0.x |

---

## 5. 数据模型（SQLAlchemy 2.0，对应规格 §48）

- `users`：id, telegram_id(unique), username, display_name, language, role, status, created_at, approved_at, disabled_at, deleted_at, last_active_at
- `user_applications`：id, telegram_id, username, message, status, reviewed_by, reviewed_at, created_at
- `tasks`：id, user_id, chat_id, url, platform, title, author, published_at, status, output_types, error_code, error_message, created_at, started_at, completed_at
- `files`：id, task_id, user_id, type, filename, size, local_path, telegram_file_id, uploaded_at, deleted_at
- `audit_logs`：id, operator_user_id, action, target_type, target_id, details, created_at
- `system_settings`：key, value, updated_by, updated_at

### 枚举

- UserRole：USER / ADMIN / SUPER_ADMIN
- UserStatus：PENDING / ACTIVE / DISABLED / DELETED
- TaskStatus：QUEUED / FETCHING / PARSING / DOWNLOADING_IMAGES / GENERATING_MARKDOWN / GENERATING_PDF / UPLOADING / COMPLETED / FAILED / CANCELLED
- OutputType：PDF / MARKDOWN / IMAGES
- FileType：PDF / MARKDOWN / IMAGES_ZIP / COVER
- 审计动作常量：USER_REGISTERED…TASK_CREATED…FILE_SENT…ADMIN_LOGIN…（规格 §39）

---

## 6. 关键流程

### 6.1 URL 下载（规格 §53）

1. bot 收到 URL → 权限检查（用户 ACTIVE）→ SSRF 校验 → detector 识别平台
2. 预览元数据（平台/标题）→ 格式选择键盘
3. 创建 task（QUEUED）→ 入队 rq → 回状态消息
4. worker：FETCHING→PARSING→DOWNLOADING_IMAGES→GENERATING_MARKDOWN→GENERATING_PDF
5. 三行摘要（excerpt，不调 LLM）
6. UPLOADING：sendDocument 上传 → 保存 telegram_file_id → 本地文件标记可删
7. COMPLETED → 发送完成消息（标题+3 行+原文链接+文件清单）

### 6.2 获取文件 vs 重新抓取（规格 §17）

- **获取文件**：查 files.telegram_file_id → sendDocument（不访问原站）
- **重新抓取**：创建新 task（旧记录保留，Version 递增）

### 6.3 存储清理（规格 §31/§32）

- 软限 800MB → 后台清理；硬限 1GB → 拒绝新任务 + 立即清理 → 到 200MB 恢复
- 优先级：已上传 Telegram > 最旧 > 最少访问 > 失败/取消任务；禁止删 PROCESSING/UPLOADING
- 上传成功后可删本地文件，历史只依赖 file_id

### 6.4 取消（规格 §54）

- QUEUED → CANCELLED 直接生效
- 运行中：worker 在检查点读 cancellation flag 尽快终止（FETCHING/PARSING/DOWNLOADING）

---

## 7. 安全设计（规格 §50）

1. **身份**：telegram_id 为核心身份；管理员用白名单 + RBAC（`ADMIN_IDS` 引导首个 SUPER_ADMIN）
2. **隔离**：所有 task/file 绑定 user_id；所有查询做 ownership check
3. **Callback 安全**：服务端解析→查库→验证身份→验证所有权→验证状态→验证文件权限→RBAC
4. **SSRF**：拒绝 127.0.0.1/localhost/内网/云 metadata；DNS 解析后二次校验
   - 三层防线：bot 入口 `validate_url` → worker 执行前复验（`run_archive` 第 0 步）→ requests 层守卫（`ssrf_guard` 劫持 `Session.send`，**重定向每一跳**都校验，覆盖 ArchiveBOT services 内部跟随 302 跳内网的绕过）
   - 残留风险：curl_cffi（知乎）与 Playwright 渲染不走 requests；DNS rebinding（校验在解析前，需 egress 网络策略兜底）
   - 豁免段：`SSRF_ALLOWED_CIDRS` 默认 `198.18.0.0/15`（Clash/sing-box 等透明代理 fake-IP DNS 段；不可公网路由，豁免不引入内网风险）
5. **限流**：重复任务、URL 数量、并发、文件大小
6. **Web Admin**：HTTPS、Session、强密码、CSRF、Rate Limit、登录审计

---

## 8. 配置（.env，规格 §45）

```
TELEGRAM_BOT_TOKEN
ADMIN_IDS                       # 逗号分隔，引导超级管理员
DEFAULT_LANGUAGE                # zh-CN | en-US | auto

DATABASE_URL                    # sqlite:///data/archivebot.db（MVP）
REDIS_URL                       # redis://redis:6379/0

STORAGE_DIR=/storage
STORAGE_SOFT_LIMIT_MB=800
STORAGE_HARD_LIMIT_MB=1024
STORAGE_CLEANUP_TARGET_MB=200

MAX_FILE_SIZE_MB=200
MAX_TASK_SIZE_MB=300
MAX_USER_CONCURRENCY=2
MAX_GLOBAL_CONCURRENCY=4
TASK_TIMEOUT_SECONDS=600
RETRY_COUNT=2

PDF_ENABLED=true
MARKDOWN_ENABLED=true
IMAGE_ENABLED=true
AI_SUMMARY_ENABLED=false

WEB_ADMIN_HOST=0.0.0.0
WEB_ADMIN_PORT=8080
WEB_ADMIN_SECRET            # session 密钥
```

---

## 9. 复用 ArchiveBOT 的边界

**复用（进程内 import）**：
- URL 解析与平台识别规则（各 service 的正则/入口函数）
- 内容提取：webpage_service（通用网页）、wechat_service、twitter_service、xhs_service、weibo_service、zhihu_service、reddit_service、youtube_service、bilibili_service、douyin_service
- media_downloader、playwright_scraper（复用其浏览器会话管理）

**不复用/不继承**：
- 其 Flask app.py 单体 Web UI（我们用 FastAPI Admin）
- 其单所有者 telegram_bot.py（我们用 aiogram 多用户网关）
- 其存储/用户模型（我们用规格 §48 的模型）

**注意**：ArchiveBOT requirements 与我们项目的依赖合并安装（readability-lxml、trafilatura、markdownify、playwright==1.58.0 等），版本冲突以本项目 pyproject 为准并在 CI 验证。

---

## 10. 可观测性

- 结构化日志（服务名、task_id、user_id、事件）
- 审计日志落库（规格 §39/§40）
- 系统状态 API 聚合：CPU/RAM/存储/队列/用户/今日任务（规格 §38）
- rq 队列监控：waiting/running/failed 计数
