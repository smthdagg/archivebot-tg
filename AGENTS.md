# AGENTS.md — Agent 协作契约

> 任何 AI Agent / 新协作者加入开发前必读。改完代码后如发现本文件与事实不符，以修复本文件为第一优先级。

## 项目是什么

多用户双语（zh-CN / en-US）Telegram 互联网文档归档与下载平台。用户发 URL → Bot 识别平台 → 选格式（PDF / Markdown / 图片 / 全部）→ 异步任务 → 收到「标题 + 3 行原文摘要 + 原始链接 + 文件」。

- 底层抓取复用 [smthdagg/ArchiveBOT](https://github.com/smthdagg/ArchiveBOT)（`vendor/ArchiveBOT` git 子模块），Telegram 网关不重复实现平台抓取逻辑。
- 设计规格拆解与里程碑：[docs/01-task-breakdown.md](docs/01-task-breakdown.md)（**进度唯一事实源，见其状态表**）。
- 架构与决策（10 条 ADR）：[docs/02-architecture.md](docs/02-architecture.md)。

## 技术栈速查

Python 3.12（硬要求 ≥3.11，用了 StrEnum）· aiogram 3.x（Bot）· FastAPI + Jinja2（Web Admin）· SQLAlchemy 2.0（SQLite，DSN 可切 PostgreSQL）· rq + Redis（队列）· Playwright Chromium（PDF）· Alembic（迁移）。

## 常用命令

```bash
# 环境（首次）
git submodule update --init --recursive
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium        # 本地跑 PDF/E2E 需要

# 质量门禁（提交前必须全绿）
.venv/bin/python -m pytest -q
.venv/bin/ruff check app/ tests/ scripts/ migrations/

# 端到端自检（不依赖 Telegram；输出 E2E-OK 为通过）
STORAGE_DIR=/tmp/ab-storage DATABASE_URL=sqlite:////tmp/ab.db \
  .venv/bin/python scripts/e2e_archive.py "https://en.wikipedia.org/wiki/Markdown"

# 数据库（模型变更后）
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic revision --autogenerate -m "描述"
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic upgrade head

# Docker（端到端 / 部署）
docker compose up --build
```

## 模块边界

| 目录 | 职责 | 改动牵连 |
|---|---|---|
| `app/bot/` | Telegram 层：handlers / keyboards / i18n / delivery | 改 callback 前缀需同步 keyboard 与 handler |
| `app/archive/` | 归档管道：detector / ssrf(+ssrf_guard) / cleaner / markdown / pdf / images / excerpt / runner / fetcher | fetcher 的平台调度表 `_DISPATCH` 决定支持范围 |
| `app/tasks/` | 队列(rq) / worker / manager(状态机/所有权) / jobs(rq 入口) | 状态机改动必须同步 `enums.py` 的 `processing_statuses()` |
| `app/storage/` | 临时文件池、800MB/1GB/200MB 清理策略 | — |
| `app/database/` | 模型 / 枚举 / 服务 | 见下方 Alembic 规则 |
| `app/admin/` | Web Admin（FastAPI + Jinja2） | 目前无 CSRF 防护，属已知待办 |
| `vendor/ArchiveBOT/` | 上游 ArchiveBOT 子模块（平台抓取服务） | **只读，永不修改** |
| `migrations/` | Alembic 迁移 | 只追加，不改历史迁移 |
| `tests/` | 单元 + process_task 链路集成测试 | — |

## 硬性规则（红线）

1. **`vendor/ArchiveBOT` 只读**。需要改上游行为时，在本仓库做包装/猴子补丁（参考 `app/archive/ssrf_guard.py`），并在 docs 里记录。
2. **改数据模型必须走 Alembic**：`alembic revision --autogenerate` 生成迁移并验证 `upgrade head`；不要用改 `init_db()` 的方式上生产。
3. **i18n 双语同步**：`app/bot/i18n/locales/{zh-CN,en-US}/messages.json` 的 key 必须成对增删，缺失 key 会静默回退 en-US。
4. **测试环境变量在导入前设置**：`tests/conftest.py` 在任何 `app.*` 导入前写入 env；新增测试文件不要在模块顶层 import app 之外自带 env 逻辑。
5. **`.env` 永不入库**；`ADMIN_IDS` 是 JSON 数组格式（`ADMIN_IDS=[123]`），不是逗号分隔。
6. **callback 安全**：所有 Telegram callback 必须服务端校验所有权（`get_task_for_user`），不能信任 callback data。
7. **SSRF**：任何新的出网代码必须过 `app/archive/ssrf.py` 校验；`ssrf_guard` 只覆盖 requests，curl_cffi/Playwright 不在守卫内（残留风险见 docs/02 §7）。
8. **PDF 页码/页眉页脚**用 Playwright `footer_template`，不要用 CSS margin box（Chromium 不支持）。
9. **完成消息的摘要不调用 LLM**（规格红线：避免幻觉），见 `app/archive/excerpt.py`。
10. **不绕过付费墙/访问控制**，只处理用户合法可访问内容（规格红线）。

## 协作流程

- **分支**：`main` 保持可发布；功能用 `feat/<主题>`，修复用 `fix/<主题>`，文档用 `docs/<主题>`；完成后合回 main。
- **提交信息**：沿用现有风格 `type: 中文摘要` + 空行 + 正文列点（type: feat/fix/chore/docs/test/ci）。
- **完成的定义（DoD）**：pytest 全绿 + ruff 全绿 + 涉及 schema 时 `alembic upgrade head` 通过 + 涉及管道时 `scripts/e2e_archive.py` 输出 E2E-OK。
- **进度同步**：完成/认领任务后**必须**更新 [docs/01-task-breakdown.md](docs/01-task-breakdown.md) 顶部状态表（含 commit 引用），这是多 Agent 之间的唯一进度事实源。
- **决策记录**：引入新技术选型或改变既有决策时，在 docs/02 追加 ADR，而不是只在 commit message 里说明。
- **部署到 VPS（生产同步）**：**任何功能/修复合入 main 后，必须执行 `scripts/deploy-to-vps.sh` 将代码同步到 VPS（/opt/archivebot）**。涉及数据库模型变更时，先在 VPS 执行 `alembic upgrade head`（见 [docs/07-vps-deployment.md](docs/07-vps-deployment.md) §8）。部署后确认四个容器 Up 且 `curl http://127.0.0.1:8080/healthz` 返回 `{"ok":true}`。VPS 部署细节、访问方式、运维命令见 [docs/07-vps-deployment.md](docs/07-vps-deployment.md)。

## 当前状态速览

以 docs/01 状态表为准。摘要：M0-M6 已完成并端到端验证（真实抓取→PDF/Markdown/图片→rq 队列→Telegram 交付边界）；M7 安全加固完成 SSRF 纵深/50MB 预检/CI；待办：真实 token 的交付联调、Cookie Profile、视频平台（Phase 2 起步）。
