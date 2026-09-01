# ArchiveBOT-TG

多用户双语 Telegram 互联网文档归档与下载平台。

**核心体验**：发送 URL → 选择 PDF / Markdown / 图片 / 全部 → 等待处理 → 收到「标题 + 3 行原文 + 原始链接 + 文件」→ 历史记录永久保存 → 服务器临时文件自动清理。

基于 [smthdagg/ArchiveBOT](https://github.com/smthdagg/ArchiveBOT)（omnisaver）做底层内容抓取与平台适配，Telegram 网关不重复实现平台抓取逻辑。

> 详细设计见 [docs/01-task-breakdown.md](docs/01-task-breakdown.md)（任务梳理）与 [docs/02-architecture.md](docs/02-architecture.md)（技术架构）。

---

## 功能

- **Telegram Bot**：中英文双语、注册审批流、URL 自动识别平台、格式选择、任务状态、三行原文摘要（不调用 LLM，避免幻觉）、完成消息自动推送文件、历史记录（分页/搜索/详情/重新发送/重新抓取/删除）、用户设置
- **平台支持**：微信公众号、X/Twitter、小红书、微博、知乎、Reddit、YouTube、Bilibili、抖音/TikTok、快手、Instagram、通用网页（经 ArchiveBOT 平台适配层）
- **多用户与权限**：USER / ADMIN / SUPER_ADMIN，用户之间严格隔离，所有 callback 服务端校验所有权
- **Web Admin**：FastAPI + Jinja2，Dashboard / Users / Tasks / Logs，Session 登录 + 登录审计
- **临时存储**：软限 800MB / 硬限 1GB / 清理目标 200MB；上传 Telegram 成功后本地文件可删，历史只依赖 `telegram_file_id`
- **安全**：SSRF 防护（拒绝内网/环回/云 metadata）、callback 越权防护、并发限制、RBAC

---

## 快速开始（Docker）

前置：Docker、`cp .env.example .env` 并填写：

```bash
TELEGRAM_BOT_TOKEN=xxxx          # @BotFather 获取
ADMIN_IDS=123456789              # 引导为 SUPER_ADMIN 的 Telegram 数字 ID
WEB_ADMIN_PASSWORD=strong-pass   # Web Admin 登录密码
```

启动：

```bash
docker compose up --build
```

服务：

| 服务 | 说明 |
|---|---|
| `bot` | Telegram 长轮询（aiogram 3.x） |
| `api` | Web Admin（http://localhost:8080/admin） |
| `worker` | Redis 队列消费者（rq），执行抓取/生成/上传 |
| `redis` | 队列 |

---

## 本地开发

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
# 运行测试（不需要 Redis/Telegram）
.venv/bin/python -m pytest tests/
```

## 目录结构

```
app/
├── bot/        # Telegram 层：handlers / keyboards / i18n / middleware
├── archive/    # 归档核心：detector / runner / cleaner / markdown / pdf / images / excerpt / ssrf
├── tasks/      # 任务系统：queue(rq) / worker / manager / jobs
├── storage/    # 临时文件池与清理
├── database/   # SQLAlchemy 模型 / 枚举 / 服务
├── admin/      # Web Admin（FastAPI + Jinja2）
└── config.py   # pydantic-settings 配置
vendor/ArchiveBOT/   # git 子模块（ArchiveBOT 平台适配，pin 上游版本）
storage/             # 运行期临时文件池（volume）
data/                # SQLite 数据（volume）
```

---

## 状态

当前为 **MVP 第一阶段骨架**（对应任务梳理 M0-M6）：

- ✅ 项目脚手架、配置、Docker、git（ArchiveBOT 子模块）
- ✅ 数据层（users / applications / tasks / files / audit_logs / settings）
- ✅ i18n（zh-CN / en-US）
- ✅ 归档管道（detector / ssrf / cleaner / excerpt / markdown / pdf / images / runner，接 ArchiveBOT）
- ✅ 任务系统（Redis 队列、worker、状态机、取消、并发限制）
- ✅ Telegram Bot（/start、审批流、URL 下载、格式选择、状态、历史、搜索、统计、设置、管理中心）
- ✅ Web Admin（登录、Dashboard、用户/任务/日志管理）
- ✅ 核心单元测试（31 个）

待完成（见 `docs/01-task-breakdown.md`）：登录类网站 Cookie Profile、视频平台交付、批量 URL、Telegram Forward、AI Summary、PostgreSQL 迁移、生产部署手册等。
