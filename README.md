# ArchiveBOT-TG — Telegram 互联网文档归档与下载平台

[English](README.en.md) | 简体中文

简洁、稳健的多用户归档网关：发一条链接，收一次交付。

**核心体验**：在 Telegram 私聊发任意文章/网页链接 → 识别平台 → 选 `PDF / Markdown / 长截图 / 全部` → 排队处理（可取消）→ 收到「标题 + 三行原文摘要 + 原文链接 + 文件」→ 历史可检索/重发/重新抓取/删除，本地已上传的文件自动清理。

> 底层抓取复用 [smthdagg/ArchiveBOT](https://github.com/smthdagg/ArchiveBOT)（`vendor/ArchiveBOT` 子模块），Telegram 网关不重复实现平台抓取。详细里程碑与**进度状态表**见 [docs/01-task-breakdown.md](docs/01-task-breakdown.md)，技术架构见 [docs/02-architecture.md](docs/02-architecture.md)，协作规则见 [AGENTS.md](AGENTS.md)。
>
> 适用于个人/团队的知识归档与离线阅读；遵循各站点的访问控制与付费墙规则，仅归档**用户自身可见**的内容。

## 特性（1.0）

- **Bot 体验**：双语（`zh-CN` / `en-US`，自动跟随 Telegram 语言）、`/start` 审批流、URL 自动识别与预览、`/` 快捷选格式、任务进度与取消、完成消息含原文摘要（不走 LLM，原文句子拼接，避免幻觉）
- **平台覆盖**
  - 文本：微信公众号、`x.com/twitter`、小红书、微博、知乎、Reddit、通用网页（`webpage_service`，Playwright + Readability）
  - 知乎评论区：带登录态归档时自动抓取评论区（作者/时间/赞数/回复楼），并入 PDF/长截图文末与 Markdown 交付；失败自动降级不影响正文
  - 视频：YouTube / Bilibili / 抖音 / 快手 / Instagram（`yt-dlp`，`videos/video.mp4` 单文件交付）；TikTok 未适配
  - 财新付费墙特化：反检测登录态注入、**长文分页拼接（?p1..?pN）**、黄金图找回与跨页去重、提示注入蜜罐剔除、`/m/` 手机链接归一化
  - 长截图：`PDF` 同模板的 `full_page` PNG（800px 宽、2x、超 40MB 转 JPEG）
- **历史与检索**：分页历史、关键词/平台搜索、详情、**文件重发**（`telegram_file_id` 复用，不重复抓取）、**重新抓取**（新版本）、删除
- **用户与设置**：默认输出格式、历史语言偏好、统计（今日/累计）、设置页
- **Cookie Profile（登录类站点）**：`COOKIE_PROFILES` / `COOKIE_PROFILES_FILE` 配置 `wechat / xhs / reddit / zhihu / twitter`（`auth_token+ct0`）登录态，任务按需注入（仅用于用户自己已登录的可见内容，绝不绕过控制）；`X` 交付已打通
- **反检测渲染**：登录类平台（财新/微信）使用 **Patchright**（Playwright 反检测 fork，协议层抹除自动化特征），抓取会话自动回写 cookie profile（服务端刷新登录 token 时自动跟随，与浏览器同机制）；cookie 失效时明确报 `LOGIN_REQUIRED` 而非静默交付残缺内容
- **任务自愈**：worker 崩溃/迁移遗留的卡死任务自动回收（`reap_stale_tasks`），并发限流错误友好化（`CONCURRENCY_LIMIT` 提示稍后再试）
- **Web Admin**：FastAPI + Jinja2（`http://localhost:8080/admin`），Dashboard/Users/Tasks/Logs，Session 登录、`_twitter_auth_pair` 挂点与 CSRF/限流加固
- **存储与清理**：任务目录 `tasks/<uuid>/{metadata,article.{md,pdf},cover,images/,videos/}`；软限 800MB / 硬限 1GB / 回落 200MB；已上传 Telegram 的本地文件可删，历史仅依赖 `file_id`
- **安全**：SSRF 多层防护（`ssrf_guard` + 校验）、所有 callback 服务端校验所有权、RBAC（`ADMIN_IDS → SUPER_ADMIN`）、并发限流、Web Admin 限流与锁定
- **交付质量**：每份产物命名 `标题_YYYY-MM-DD_HHMM.ext`；`Markdown` 交付改为远程图片引用（单文件可直接查看）；`PDF/长截图` 版式为“标题在上、正文在中、信息块在文末”（文末含作者/来源/发布时间/原始链接 + 免责声明，标题不重复）

## 系统要求

- Docker（含 `docker compose`）
- Python 3.12（本地开发/测试用；容器内为 `python:3.12-slim` + Playwright Chromium + CJK 字体 + `ffmpeg`）
- 一个 Telegram Bot 的 token（`@BotFather`）

## 快速开始（Docker — 推荐）

1) 准备配置：

```bash
cp .env.example .env   # 填 TELEGRAM_BOT_TOKEN、ADMIN_IDS 等
```

2) 启动全栈：

```bash
docker compose up --build
```

3) 服务：

| 服务 | 说明 |
|---|---|
| `bot` | Telegram 长轮询（`aiogram 3.x`，`python -m app.bot.main`） |
| `api` | Web Admin `http://localhost:8080/admin`（`python -m app.main`） |
| `worker` | 队列消费者 `rq`（`python -m app.tasks.worker`），`REDIS_URL=redis://redis:6379/0` |
| `redis` | `redis:8-alpine` |

首次启动会自动从 `.env` 引导第一个 `SUPER_ADMIN`（`ADMIN_IDS` 里的数字 ID）。

## 本地开发与测试

```bash
git clone --recurse-submodules https://github.com/smthdagg/archivebot-tg.git
cd archivebot-tg
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium

# 测试（不依赖 Redis/Telegram）
.venv/bin/python -m pytest -q                 # 健康跑
.venv/bin/python -m pytest -m "not slow" -q   # 排除 ≥90s 的慢鉴别用例（如 X 无授权的 96s 探测）

# Lint
.venv/bin/ruff check app/ tests/ scripts/ migrations/

# 端到端自检（真实抓取 → MD → PDF → 长截图 → 摘要，输出 E2E-OK）
STORAGE_DIR=/tmp/ab-storage DATABASE_URL=sqlite:////tmp/ab.db \
  .venv/bin/python scripts/e2e_archive.py "https://en.wikipedia.org/wiki/Markdown"
```

### 数据库迁移（Alembic）

```bash
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic revision --autogenerate -m "描述"
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic upgrade head
```

CI 在每次 `push` / `PR` 上执行 `ruff + pytest + alembic upgrade head`（`.github/workflows/ci.yml`）。

## 使用方法

### Telegram

- 发一条链接（支持 `https://x.com/.../status/...`、`https://mp.weixin.qq.com/s/...` 等），Bot 会预览平台与标题；
- 点选 `📄 PDF / 📝 Markdown / 🖼 长截图 / 📦 全部`；也可发 URL 时用文本中的 `/pdf /md /img /all` 快捷指定；
- 排队期间可「取消」；完成后收到带原文摘要的消息并自动接收文件；
- `📚 下载历史` 支持分页/搜索/详情；已落盘的 `telegram_file_id` 可直接「获取文件」，或「重新抓取」生成新版本；
- X 这类需登录才可见的推文，请先用 **Cookie Profile** 注入你的 `x.com` 登录态（见下），否则会在队列完成后落 `FAILED / LOGIN_REQUIRED` 的可读报错，不会误标 `UNKNOWN`。

### Cookie Profile（登录类站点）

仅用于**用户自己已登录的可见内容**，不在任何链路自动附加：

- 配置源：`COOKIE_PROFILES`（inline JSON）与/或 `COOKIE_PROFILES_FILE`（JSON 文件），两者合并、文件优先；
- 格式（`Cookie-Editor`，每平台为一个 `list`）：`{ "<profile 名>": { "<平台>": [ {"name","value","domain","path"} ] } }`；
- 已修通平台：`wechat / reddit`（文件型 `_COOKIES_PATH`）、`zhihu / twitter / xhs`（方法型，`twitter` 取 `auth_token+ct0` 双域种 cookie）；`web / weibo` 不支持注入（记录在案）；
- X 示例：从浏览器导出 `auth_token` 与 `ct0`（`.x.com` 与 `.twitter.com` 同时种）制作为名为 `x` 的 Profile，任务以 `cookie_profile="x"` 注入；Bot 对 `twitter` 平台若本地存在 `x/twitter` 会自动关联；
- 详表与红线见 [docs/05-cookie-profile.md](docs/05-cookie-profile.md)。

### 部署到自己的 VPS

```bash
# 私有连接参数写在 scripts/deploy.env.local（已 gitignore）：
#   VPS_HOST=<你的VPS地址>
#   VPS_PORT=<SSH端口>
./scripts/deploy-to-vps.sh                 # 门禁 → 打包 → 上传 → 构建 → 重启 → 健康检查
./scripts/deploy-to-vps.sh --skip-build    # 仅代码/模板改动时跳过镜像重建
```

脚本会排除 `.env` / `data` / `storage`（VPS 上的生产配置与数据不被覆盖）。

## 配置

常用 `.env` 键（亦见 [.env.example](.env.example)）：

```
TELEGRAM_BOT_TOKEN=              # @BotFather
ADMIN_IDS=[123456789]            # 引导为 SUPER_ADMIN 的 Telegram ID（JSON 数组）
DEFAULT_LANGUAGE=auto            # zh-CN | en-US | auto
DATABASE_URL=sqlite:///data/archivebot.db
REDIS_URL=redis://redis:6379/0
STORAGE_DIR=/storage
COOKIE_PROFILES=                 # inline JSON（可选）
COOKIE_PROFILES_FILE=data/cookie_profiles.json
WEB_ADMIN_HOST=0.0.0.0
WEB_ADMIN_PORT=8080
WEB_ADMIN_SECRET=change-me-to-a-long-random-string
WEB_ADMIN_PASSWORD=change-me-strong-password
```

## 文档

- [AGENTS.md](AGENTS.md) — 模块边界、硬性规则（`vendor` 只读、SSRF、i18n 成对、callback 校验等）、协作流程与 DoD
- [docs/01-task-breakdown.md](docs/01-task-breakdown.md) — 里程碑 M0–M9 状态表（唯一事实源），M2 已含 `web/wechat/财新分页/X` 的真实验证注明
- [docs/02-architecture.md](docs/02-architecture.md) — 架构与 10 条 ADR、数据模型、关键流程、安全设计
- [docs/03-acceptance.md](docs/03-acceptance.md) / [docs/04-deployment.md](docs/04-deployment.md) / [docs/05-cookie-profile.md](docs/05-cookie-profile.md)
- [docs/07-vps-deployment.md](docs/07-vps-deployment.md) — **当前 VPS 实际部署实例**（`/opt/archivebot`：容器组成、访问方式、运维命令、安全封锁、共存服务）

## 版本

### 1.0.1（最新）

- **反检测渲染**：财新/微信切换 Patchright；财新会话自动回写，解决登录态被风控反复作废的问题
- **X 平台交付打通**：Tweet → 归档产物桥接，`LOGIN_REQUIRED` 错误码一致
- **并发自愈**：僵尸任务超时回收 + `CONCURRENCY_LIMIT` 友好提示
- **版式**：PDF/长截图标题与作者行居中；信息块（作者/来源/时间/链接）移至文末
- **运维**：`deploy-to-vps.sh` 部署契约（`.env` 不覆盖）、每日备份、健康监控（4 容器 + healthz）、日志轮转、清理脚本与业务数据解冲突

### 1.0.0

- 版本：`1.0.0`（`pyproject.toml` / `v1.0.0` tag）
- 核心能力：全文归档（`PDF / Markdown / 长截图`）+ 队列交付 + 历史与 Web Admin + SSRF/RBAC/文件大小与并发防护
- 重点修复（1.0 周期）：财新分页与正文清理（黄金图找回与去重、AI 组件与重复标题剔除、版式“标题在上、信息块在文末”）、微信公众号的空白截图与本地图片映射、`X` 的联调与 `LOGIN_REQUIRED` 错误码一致、交付 `Markdown` 的单文件可视化（远程图片引用）、菜单选中即收起与终态按钮清理
- 验证：全门禁健康跑 `156` 用例（`-m "not slow"` 排除慢鉴别），`ruff + pytest + alembic` 全绿；真实抓取路径端到端验证为**已验证交付**的平台集合（`web/wechat` 全链路，`twitter` 在授权 Profile 下已验证，非验证平台仅作接线登记，文档作鉴别说明）
- 已知局限：`TikTok` 未适配；部分 `X` 推文在匿名上下文需登录态（需自备 `auth_token/ct0` Profile）；`AGENTS.md` 与 `docs/03` 的验收清单仍以 M2/M8 的已验证范围为准

## 目录结构

```
app/
  bot/        # Telegram 层：handlers / keyboards / i18n / middleware / delivery
  archive/    # 归档核心：detector / runner / cleaner / markdown / pdf / screenshot / images / excerpt / fetcher / ssrf(+guard) / cookie_profile
  tasks/      # 任务系统：queue(rq) / worker / manager / jobs
  storage/    # 临时文件池与清理（800MB/1GB/200MB）
  database/   # SQLAlchemy 模型 / 枚举 / 服务
  admin/      # Web Admin（FastAPI + Jinja2）
  config.py   # pydantic-settings

vendor/ArchiveBOT/   # git 子模块（平台抓取服务，pin 上游版本，只读）
storage/              # 运行期临时文件池（volume）
data/                 # SQLite 数据（volume，gitignore）
tests/  migrations/  scripts/  docs/
```

## 许可证

未单独声明许可证；默认保留所有权利。如需开源许可，请在发布前添加 `LICENSE` 并同步本节。
