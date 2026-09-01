# ArchiveBOT-TG 验收清单（Acceptance Checklist）

> **里程碑：M8 测试与验收**
> 本文档把设计规格（`docs/01` 各里程碑清单）逐条映射为可验收项，标注**已实现 / 部分 / 未实现**三态，并注明实现位置（`文件:函数`）与对应测试或验证方式。
> 进度唯一事实源：[docs/01-task-breakdown.md](01-task-breakdown.md) 顶部状态表。本文档与状态表可能冲突时，以「代码现状 + 实测」为准（本文档反映的是**逐文件核对后的实际状态**，而非状态表的乐观估计）。

---

## 0. 术语与图例

- **状态**：✅ 已实现（代码存在且被测试覆盖或人工验证）｜🔶 部分（主路径在但缺某环节/未接线/未联调）｜❌ 未实现（无代码）。
- **实现位置**：`文件:函数`（无特殊说明均指 `app/` 下）。
- **验证**：测试用例路径（`tests/test_*.py`）或人工/脚本验证（`scripts/e2e_archive.py`、真实环境部署）。
- 三态的判定依据是**这次逐文件核对 + 实测 pytest（42 passed）**，不是历史 commit 说明。

**本轮核对的总体结论（与历史事实修正）**：

1. **仓库缺陷已修复**：`.gitignore` 的 `storage/` 规则同时屏蔽了源码模块 `app/storage/`（M5 存储管理），导致该模块从未入库——任何 fresh checkout / CI 都会 `ModuleNotFoundError: No module named 'app.storage'`（已实测复现）。本次已将规则改为 `/storage/`（锚定仓库根）并纳入版本控制，fresh checkout 的 `pytest` 恢复 42 passed。
2. **CSRF / 重试 / 限流三项经核对均未实现**（不是已做）：
   - CSRF：`app/admin/auth.py` 与 `app/admin/routes.py` 均无 CSRF token 校验；
   - 自动重试：`config.retry_count` 已配置但 `app/tasks/jobs.py` 无任何消费/重入逻辑；
   - 限流：`app/bot/middleware/__init__.py` 为空文件，无逐用户节流；Web Admin 无 Rate Limit。
   因此在下文一律标 ❌，并在 §9 待办列出。

---

## 1. M0 项目基础设施

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| Git 仓库与目录结构 | ✅ | 仓库根；`docs/02 §3` 目录结构与实机一致 | `git ls-files` 核对 |
| 分支约定（main 可发布 / feat/fix/docs 功能分支） | 🔶 | 分支实践存在；无 `develop` 严格流程 | `git branch` |
| `pyproject.toml` 依赖锁定 | ✅ | `pyproject.toml` + `uv.lock` | `uv.lock` 存在、`pip install -e ".[dev]"` |
| `.env.example` 全量环境变量模板 | ✅ | `.env.example` | 覆盖 §45 全部键 |
| 配置加载与校验 | ✅ | `app/config.py:Settings` / `get_settings`（pydantic-settings，含 `default_language` 校验） | `tests/test_config.py` |
| `Dockerfile` + `.dockerignore`（3.12 + Playwright/Chromium） | ✅ | `Dockerfile`、`.dockerignore` | `docker compose build`（需环境） |
| `docker-compose.yml`（bot/api/worker/redis + 卷） | ✅ | `docker-compose.yml` | 结构核对 |
| 基础日志设施（结构化/文件轮转） | ❌ | —（仅 stdlib `logging`，无结构化与轮转） | 代码核对 |
| Makefile / dev 脚本 | ❌ | — | 代码核对 |
| CI（ruff + pytest + alembic） | ✅（本次修复后） | `.github/workflows/ci.yml` | ruff、pytest、`alembic upgrade head` 三步齐全。**此前因 `app/storage` 被 gitignore 屏蔽必挂，已修复** |
| **验收**：`docker compose up` 全服务无配置报错 | 🔶 | — | 需真实环境 docker 启动（本机未启动验证）；已随历史 commit 容器内验证过 M0-M6 |

## 2. M1 数据层与国际化

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| 六张模型：users / user_applications / tasks / files / audit_logs / system_settings | ✅ | `app/database/models.py:User/UserApplication/Task/File/AuditLog/SystemSetting` | `tests/test_database.py` |
| 引擎 / Session / DSN 可切换 PostgreSQL | ✅ | `app/database/database.py:_make_engine/init_db/get_session`；`app/config.py:is_sqlite`；依赖含 `psycopg[binary]` | `tests/test_config.py:test_bytes_properties` |
| Alembic 迁移 | ✅ | `migrations/versions/4afa672b2a1c_initial_schema.py`、`alembic.ini` | `alembic upgrade head`（含 CI） |
| 中英双语 `messages.json` | ✅ | `app/bot/i18n/locales/{zh-CN,en-US}/messages.json` | `tests/test_i18n.py` |
| i18n 加载器（按语言取文案、auto 回退） | ✅ | `app/bot/i18n/__init__.py:resolve_language/t` | `tests/test_i18n.py` |
| 枚举集中定义（无魔法字符串散落） | ✅ | `app/database/enums.py:UserRole/UserStatus/TaskStatus/OutputType/FileType/AuditAction/Platform/ErrorCode` | `tests` 内引用枚举（Grep 核对） |
| **验收**：CRUD + 双语切换 + 枚举无散落 | ✅ | 同上 | `pytest tests/test_database.py tests/test_i18n.py` |

## 3. M2 归档核心管道

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| `vendor/ArchiveBOT` git 子模块（pin 版本） | ✅ | `.gitmodules`、`vendor/ArchiveBOT` | 子模块初始化 |
| 复用 ArchiveBOT `services.*` 抓取（web/wechat/x/xhs/weibo/zhihu/reddit） | ✅ | `app/archive/fetcher.py:fetch_article/_DISPATCH`（7 平台已接线） | 容器 E2E-OK（`scripts/e2e_archive.py`） |
| 视频平台抓取（youtube/bilibili/douyin/tiktok/kuaishou/instagram 等） | ❌ | `fetcher._DISPATCH` 无条目；detector 能识别但 fetch 抛 `ErrorCode.UNKNOWN` | `tests/test_detector.py` 仅覆盖识别 |
| URL→平台识别 | ✅ | `app/archive/detector.py:detect`（覆盖 web 通用 + 14 平台域名） | `tests/test_detector.py` |
| 正文清洗（去导航/广告/脚本/Cookie banner/评论区等） | ✅ | `app/archive/cleaner.py:clean_html/clean_text`（基于 readability/trafilatura） | `tests/test_cleaner.py` |
| 规范化 Markdown（图片本地化/占位） | ✅ | `app/archive/markdown.py:html_to_markdown/rewrite_image_refs/build_markdown_file` | `scripts/e2e_archive.py`；`tests/test_jobs_integration.py` |
| Chromium Print-to-PDF（标题/作者/来源/时间/原文链接/页码/归档页脚） | ✅ | `app/archive/pdf.py:build_pdf`；**用 Playwright `footer_template`，无 CSS margin box** | `tests/test_pdf.py`（模板/FNV 断言） |
| 图片下载 / 封面提取 / ZIP | ✅ | `app/archive/images.py:copy_images/build_image_map/make_images_zip` | `scripts/e2e_archive.py` |
| 三行原文摘要（不调用 LLM、无幻觉） | ✅ | `app/archive/excerpt.py:extract_excerpt`（取前三个有效句子） | `tests/test_excerpt.py` |
| 任务编排状态机（FETCHING→…→产出） | ✅ | `app/archive/runner.py:run_archive`（0-8 步） | `tests/test_jobs_integration.py` |
| SSRF 前置防护 | ✅ | `app/archive/ssrf.py:is_safe_host/validate_url` | `tests/test_ssrf.py` |
| 登录/JS 网站（Playwright + 独立 Cookie Profile） | ❌ | —（预留 Phase 2） | — |
| **验收**：真实 URL「提取→清洗→MD→PDF→图片→摘要」 | ✅/🔶 | `scripts/e2e_archive.py` | 历史容器内 E2E-OK；本机需 Chromium/网络重跑 |

## 4. M3 任务系统

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| Redis + rq 队列 | ✅ | `app/tasks/queue.py:get_queue/enqueue_task/queue_stats` | 结构核对（需 Redis 实例联调） |
| worker 消费 → runner → DB 状态 → 状态消息 | ✅ | `app/tasks/worker.py:main`、`app/tasks/jobs.py:process_task` | `tests/test_jobs_integration.py` |
| 任务 CRUD / 所有权校验 / 取消 / 并发限制 | ✅ | `app/tasks/manager.py:create_task/get_task_for_user/request_cancel/is_cancelled/user_active_task_count/global_active_task_count` | `tests/test_database.py`（ownership） |
| 状态机 QUEUED→…→COMPLETED / FAILED / CANCELLED | ✅ | `app/database/enums.py:TaskStatus`；`jobs.py:on_status` | `tests/test_database.py:test_task_status_transitions`、`test_jobs_integration.py` |
| 进度状态消息编辑 | ✅ | `jobs.py:_update_status_message` | 集成测试路径 |
| 失败分类（404/403/超时/空/图片/PDF/上传/存储） | ✅ | `fetcher.py:_classify/_classify_code`；`jobs.py:_fail/_error_text` | 代码核对 |
| Telegram `sendDocument` 上传 + `telegram_file_id` 落库 + 本地可删 | ✅ | `jobs.py:_upload_all`；`app/bot/delivery.py:send_document`；`jobs.py:_cleanup_local` | `tests/test_jobs_integration.py`（含 50MB 跳过） |
| **失败自动重试**（`retry_count`） | ❌ | `config.retry_count=2` 已配置；`jobs.py` 无消费/重入逻辑 | 代码核对（Grep 无 retry 调用） |
| 超时控制 | 🔶 | `config.task_timeout_seconds`；`queue.enqueue_task(job_timeout=1800)` | 仅 rq 级；无每任务细粒度超时执行 |
| **验收**：入队→完成 / 重试 / 取消 | 🔶 | — | 完成✅；重试❌；取消路径无单测（`_Cancelled` 仅在代码内） |

## 5. M4 Telegram 用户端

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| `/start` 创建用户/申请（PENDING）+ 双语欢迎 | ✅ | `app/bot/handlers/start.py:on_start/_create_application`；`app/bot/common.py:ensure_user` | 代码核对 |
| 主菜单（新建/历史/搜索/统计/设置/帮助/管理中心） | ✅ | `app/bot/keyboards/__init__.py:main_menu`；`handlers/menu.py` | 代码核对 |
| URL 下载：识别平台 + 预览元数据 + 格式选择 + 建任务 | ✅ | `handlers/archive.py:on_url_message/on_format_selected`；`keyboards.format_selector` | 代码核对 |
| 状态消息 + [取消] 按钮 | ✅ | `keyboards.cancel_button`；`handlers/archive.py:on_cancel`；`jobs._update_status_message` | 代码核对 |
| 完成消息（标题/来源/作者/发布时间/3 行/原文链接/产出清单）+ 自动发文件 | ✅ | `jobs.py:_completion_text` + `delivery` | `tests/test_jobs_integration.py`（交付目标） |
| 失败消息（原因/平台/URL）+ [重试][打开原文] | 🔶 | 失败消息✅ `jobs.py:_fail`；`[打开原文]`✅ `history.open_original`；**`[重试]`按钮无处理** | 代码核对 |
| 历史列表/分页/详情/获取文件/重新抓取/删记录 | ✅ | `handlers/history.py:history_list/page/detail/get_file/rearchive/delete_record` | 代码核对 |
| 「获取文件」走 file_id 不访问原站；「重新抓取」建新 task | ✅ | `history.get_file` / `history.rearchive` | 代码核对 |
| 历史搜索（标题/URL/平台/作者/关键词） | ✅ | `handlers/menu.py:search_prompt/search_execute` | 代码核对 |
| 我的统计（时间/平台/格式维） | ✅ | `handlers/menu.py:stats` | 代码核对 |
| 设置（默认格式/摘要/PDF 选项/语言） | 🔶 | `handlers/menu.py:settings` 入口存在；配置项持久化未深核 | 代码核对 |
| 对话隔离（所有查询强校验 `user_id`） | ✅ | `manager.py:get_task_for_user`（history/archive/admin 均经此） | `tests/test_database.py`（ownership） |
| **验收**：规格 §61 Telegram 逐条（中英/选格式/状态/3 行/原文/自动发/历史分页搜索/重发/重抓） | 🔶 | — | **需真实 `TELEGRAM_BOT_TOKEN` 交付联调**（docs/01 M4 待办，未做） |

## 6. M5 存储管理

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| `/storage/tasks/<uuid>/{metadata.json,article.md,article.pdf,cover.jpg,images/}` | ✅ | `app/storage/manager.py:StorageManager.new_task_dir` | `tests/test_storage.py` |
| 软限 800MB / 硬限 1GB / 清理目标 200MB（可配置） | ✅ | `app/config.py:storage_soft_limit_mb/hard/cleanup_target`；`manager.soft_limit/hard_limit/cleanup_target/over_soft/over_hard` | `tests/test_config.py` |
| 用量统计 | ✅ | `manager.py:total_size/task_size` | `tests/test_storage.py` |
| 清理策略（最旧优先、禁删受保护 PROCESSING/UPLOADING、滑到目标） | ✅ | `app/storage/cleanup.py:CleanupService.run_cleanup` | `tests/test_storage.py:test_cleanup_deletes_oldest_and_protects_active` |
| 硬限禁止新大任务 | ✅ | `manager.create_task` 调 `can_accept_new_task` → 抛 `STORAGE_FULL` | `tests/test_jobs_integration.py` |
| **软限后台自动清理**（达到软限触发） | ❌ | `cleanup.cleanup_if_needed` 已定义但**无任何运行时/调度调用** | Grep 核对（仅定义处） |
| 完成后删本地、DB 保留 file_id | ✅ | `jobs.py:_cleanup_local` | 代码核对 |
| 模块已入库（不再是 gitignore 孤儿） | ✅ | `app/storage/{manager,cleanup}.py`（**本次从 gitignore 屏蔽中恢复**） | fresh checkout `pytest` 42 passed |
| **验收**：模拟塞满 1GB → 触发清理恢复 200MB | 🔶 | — | 仅单测覆盖 `CleanupService` 逻辑；未做真实 1GB 模拟 |

## 7. M6 管理后台

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| RBAC USER / ADMIN / SUPER_ADMIN | ✅ | `app/database/enums.py:UserRole`；`keyboards.is_admin_role` | 代码核对 |
| `ADMIN_IDS` 引导首个超级管理员 | ✅ | `app/config.py:admin_ids`；`start.py:_is_admin` | 代码核对 |
| Bot 管理中心（用户/待审核/任务/文件/系统状态/日志/设置） | 🔶 | `handlers/admin.py:admin_center/pending_list/application_detail/review_application/system_status/logs`；文件管理/系统设置入口未齐 | 代码核对 |
| 审核流：批准/拒绝/禁用/恢复/删除 + 审计 | ✅ | `handlers/admin.py:review_application`；`routes.user_action`；`services.audit` | 代码核对 |
| Web Admin（FastAPI + Jinja2） | ✅ | `app/admin/routes.py`；`templates/{base,dashboard,login,logs,tasks,users}.html` | 路由核对 |
| Web 登录：Session 认证 + 登录审计 | ✅ | `app/admin/auth.py:create_session/read_session/login_required/verify_password/log_login`（itsdangerous 签名 Cookie、24h） | 代码核对 |
| **Web 登录 CSRF 防护** | ❌ | `routes.login_submit`/`user_action` 无 CSRF token | 代码核对 |
| 审计日志落库（User/Task/File/Admin Log） | ✅ | `models.py:AuditLog`；`enums.py:AuditAction`；`services.audit`；`routes.logs` | 代码核对 |
| 系统状态：CPU/RAM/存储/队列/用户/今日任务 | 🔶 | `routes.dashboard` 聚合存储/队列/用户/今日任务/失败；**无 CPU/RAM 指标、无独立 API 端点** | 代码核对 |
| **验收**：管理操作全走审计、可在 Bot 与 Web 完成管理 | 🔶 | — | 需真实运行验证 |

## 8. M7 安全加固

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| SSRF 三层防线：bot 入口 / worker 复验 / requests 层守卫 | ✅ | 入口 `handlers/archive.py:on_url_message` + `manager.create_task` 前置校验；复验 `runner.py:run_archive:42`；requests 层 `fetcher.fetch_article:121`(`ensure_installed`) + `ssrf_guard.BlockedHostError`，**重定向每跳复验** | `tests/test_ssrf.py`、`tests/test_ssrf_guard.py`、`tests/test_jobs_integration.py:test_process_task_ssrf_rejected_by_worker` |
| callback 安全（解析→查库→身份→所有权→状态→文件权限→RBAC） | ✅ | `manager.get_task_for_user` 统一强制；各 handler 均经此 | `tests/test_database.py`（ownership） |
| 越权防护（file_id/详情只给所属用户） | ✅ | `history.get_file/history_detail/rearchive` 经 `get_task_for_user` | 代码核对 |
| 防重复任务 | ❌ | —（无 dedup 逻辑） | Grep 核对 |
| URL 数量限制 | ❌ | — | Grep 核对 |
| 并发限制（单用户 2 / 全局 4，可配置） | ✅ | `jobs._process` 校验；`config.max_user_concurrency/max_global_concurrency` | 代码核对 |
| 文件大小限制（服务端，不信任 callback） | ✅ | `jobs._upload_all` 50MB 预检跳过；`config.telegram_max_file_mb/max_file_size_mb/max_task_size_mb` | `tests/test_jobs_integration.py:test_process_task_oversized_pdf_skipped` |
| Web Admin：HTTPS / Session / 强密码 / CSRF / Rate Limit | 🔶 | Session✅ 强密码✅(compare_digest) 登录审计✅；HTTPS 需部署反代未配置；**CSRF❌ RateLimit❌** | 代码核对 |
| **限流（逐用户节流）** | ❌ | `app/bot/middleware/__init__.py` 为空 | 代码核对 |
| 管理员操作服务端再验证 | ✅ | `routes.user_action` 服务端重查用户再执行 | 代码核对 |
| **验收**：恶意 callback / 越权 file_id / 内网 SSRF 全部拦截 | 🔶 | — | SSRF 拦截✅ 有单测；**恶意 callback 越权无专项测试** |

## 9. M8 测试与验收（本文档）

| 验收项 | 状态 | 实现位置 / 说明 | 验证 |
|---|---|---|---|
| 单元测试：detector/cleaner/excerpt/storage/config/i18n | ✅ | `tests/test_detector.py`、`test_cleaner.py`、`test_excerpt.py`、`test_storage.py`、`test_config.py`、`test_i18n.py` | `pytest` 全绿 |
| 数据库测试：CRUD / ownership | ✅ | `tests/test_database.py` | 同 |
| 集成测试：队列→worker→DB 状态流转（mock 抓取/上传） | ✅ | `tests/test_jobs_integration.py`（happy / 50MB 跳过 / SSRF 拒绝） | 同 |
| PDF 模板断言（无 margin box、页脚 Playwright 计数器） | ✅ | `tests/test_pdf.py` | 同 |
| Bot handler 测试（aiogram mock Update） | ❌ | — | 无 |
| 安全测试：SSRF✅；callback 越权专项 | 🔶 | `test_ssrf.py`、`test_ssrf_guard.py` | SSRF 有；callback 越权无 |
| 按规格 §61 逐条核验并记录 | 🔶 | **本文档** + 真实 token §61 联调待做 | 待 `TELEGRAM_BOT_TOKEN` |
| CI：lint + test + build | 🔶 | `.github/workflows/ci.yml`（ruff + pytest + alembic）；**无 build 步骤** | CI 需修复后首次 green |
| **验收**：`pytest` 全绿 | ✅ | 实测 **42 passed**（primary 与修复后的工作区） | `DATABASE_URL=… .venv/bin/python -m pytest -q` |

## 10. M9 部署与文档

| 验收项 | 状态 | 实现位置 | 验证 |
|---|---|---|---|
| docker-compose 生产配置（卷/健康检查/重启策略/日志轮转） | 🔶 | `docker-compose.yml`：卷✅ 重启(**unless-stopped**)✅；**健康检查❌ 日志轮转❌** | 结构核对 |
| `.env.example` 完整注释 + 部署手册（VPS，规格 §52） | 🔶 | `.env.example`✅ 注释齐全；**VPS 部署手册未写** | 代码核对 |
| Alembic 迁移（SQLite→PostgreSQL 可切换） | ✅ | `migrations/`、`config.is_sqlite`、`psycopg[binary]` | `alembic upgrade head` |
| Playwright/Chromium 容器内安装与缓存 | ✅ | `Dockerfile` | `docker compose build` |
| 运维手册：日志/监控/备份（DB+redis）/升级 | ❌ | — | — |
| README（功能/快速开始/目录/开发指南） | ✅ | `README.md`、`AGENTS.md`、`docs/01`、`docs/02` | 内容核对 |
| **验收**：全新 VPS 按文档 15 分钟跑通 MVP | ❌ | — | 未验证 |

---

## 11. 已知待办（复杂，需要单独任务）

以下为本轮核对确认**未实现**或未联调的项目，按影响排序：

1. **交付联调**（P0）：真实 `TELEGRAM_BOT_TOKEN` + `docker compose up --build` 打通 §13 全链路并过规格 §61 逐条（M4 遗留）——目前所有 Bot handler 均未在真实网关验证。
2. **Web Admin CSRF**（P0，M6/M7 遗留）：`app/admin/routes.py` 的弱校验表单（登录、`/users/{id}/action`）需加 CSRF token，当前无。
3. **自动重试**（M7 遗留）：`config.retry_count=2` 存在但 `app/tasks/jobs.py` 无消费逻辑；失败任务需按配置重入队列。
4. **限流 / 防重复 / URL 数量限制**（M7 遗留）：`app/bot/middleware/__init__.py` 为空；Web Admin 无 Rate Limit。
5. **软限后台自动清理**（M5）：`cleanup.cleanup_if_needed` 已实现但未接线到 worker/调度。
6. **视频平台交付**（Phase 2）：detector 已识别 youtube/bilibili/douyin 等，但 `fetcher._DISPATCH` 无条目；另需 Cookie Profile 登录网站。
7. **系统状态 CPU/RAM 指标、独立状态 API**（M6）。
8. **部署/运维手册 + VPS 15 分钟验收**（M9）；docker-compose 健康检查与日志轮转。
9. **Bot handler 单测、恶意 callback 越权专项测试**（M8 补齐）。

> 说明：任务描述中「CSRF 已做 / 重试已做 / 限流已做则标已实现」——经核对这三项**实际均未实现**，故上文据实标注 ❌，未做乐观标记。

---

*验收日期：2026-09-01。实测基线：`pytest -q` → `42 passed`；`ruff check app/ tests/` 全绿；`.gitignore` 缺陷修复后 fresh checkout 可正常收集。*