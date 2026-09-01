# ArchiveBOT-TG 任务梳理（Task Breakdown）

> 多用户双语 Telegram 互联网文档归档与下载平台。
> 基于 [smthdagg/ArchiveBOT](https://github.com/smthdagg/ArchiveBOT)（omnisaver fork）做底层内容抓取与平台适配。
> 本文档把设计规格拆解为可执行、可验收的里程碑任务，作为开发管理基线。

---

## 0. 总览

| 里程碑 | 名称 | 交付物 | 优先级 |
|---|---|---|---|
| M0 | 项目基础设施 | 仓库、脚手架、Docker、CI | P0 |
| M1 | 数据层与国际化 | 数据库模型、迁移、i18n | P0 |
| M2 | 归档核心管道 | ArchiveBOT 集成、detector/cleaner/markdown/pdf/images | P0 |
| M3 | 任务系统 | Redis 队列、worker、状态机、取消、并发限制 | P0 |
| M4 | Telegram 用户端 | /start、审批、URL 流程、格式选择、状态、完成消息、历史、搜索、设置 | P0 |
| M5 | 存储管理 | 临时文件池 1GB、清理到 200MB | P0 |
| M6 | 管理后台 | Bot 管理中心、Web Admin、RBAC、审计日志 | P0 |
| M7 | 安全加固 | SSRF、callback 校验、越权防护、限流 | P0 |
| M8 | 测试与验收 | 单元/集成测试、验收清单 | P0 |
| M9 | 部署与文档 | docker-compose、README、运维手册 | P0 |

后续阶段（设计规格 §59/§60）：批量 URL、Telegram Forward、AI Summary、平台统计、Cookie Profile 登录网站、Web 用户端、API/Webhook/RSS、Obsidian/Zotero 集成等 → 记为 Phase 2 / Phase 3。

---

## 0.1 状态表（多 Agent 进度唯一事实源）

> 认领/完成任务后必须更新本表（含 commit 引用）。其他进度描述（如 README）与本表冲突时，以本表为准。

**最后更新：2026-09-01（Phase 2 视频交付 t_1fb0ee77 · M9 7c00f2b · 重试 819796a · 限流 t_fd0a2028 · CSRF t_e20cd095 · 存储清理 cbfe4b2 · Bot 单测 0124b95）**

| 里程碑 | 状态 | 说明 | 关键 commit |
|---|---|---|---|
| M0 基础设施 | ✅ 完成 | 脚手架、Docker(3.12)、CI(ruff+pytest+alembic)、Alembic 迁移（**修复：CI 原因 `storage/` gitignore 屏蔽 `app/storage` 在 fresh checkout 必挂，本次修复**） | f5104fc → a8d88ef+ |
| M1 数据层与 i18n | ✅ 完成 | SQLAlchemy 模型/枚举、zh-CN/en-US 全量文案 | f5104fc |
| M2 归档核心管道 | ✅ 完成并 E2E 验证 | 文本类平台（web/微信/Reddit/X/小红书/微博/知乎）；**视频平台已接入（Phase 2 t_1fb0ee77）：youtube/bilibili/douyin/kuaishou/instagram → fetcher.fetch_video 产出 VideoResult(video_path) → runner.run_video → jobs send_video 交付（复用 50MB 预检），TikTok 尚无 ArchiveBOT service 未适配**；本地+容器内真实抓取 E2E-OK | f5104fc → 9de57a4 · t_1fb0ee77 |
| M3 任务系统 | ✅ 完成并容器内验证 | rq 队列、状态机、取消、并发限制；rq 消费全链路打通 | f5104fc → 9de57a4 |
| M4 Telegram 用户端 | ✅ 完成 | 全部 handler + i18n + 审批流；**真实 token 的交付联调待做** | f5104fc |
| M5 存储管理 | ✅ 完成 | 800MB/1GB/200MB、file_id 历史（**修复：`app/storage/` 被 `.gitignore storage/` 屏蔽未入库，已改为 `/storage/` 并纳入版本控制**；**软限后台自动清理已接线 cbfe4b2**：任务完成后查 storage.over_soft→清理到 target，保护运行中/当前目录，STORAGE_CLEANUP 审计） | f5104fc → a8d88ef+ · cbfe4b2 |
| M6 管理后台 | ✅ 完成 | Bot 管理中心 + Web Admin + 审计；**Web Admin CSRF 已补**（itsdangerous double-submit，登录与用户操作 POST 校验，无/错 token → 403，test_admin_csrf.py） | f5104fc → t_e20cd095 |
| M7 安全加固 | 🔶 大部分完成 | SSRF 三层防线/50MB 预检/RBAC/所有权校验 ✅；**失败自动重试（819796a）、Web Admin 限流+登录锁定（t_fd0a2028）、CSRF（t_e20cd095）均已实现** | 9de57a4 → ea4dc1a |
| M8 测试与验收 | ✅ 大部分完成 | 104 个测试（单元+集成+**Bot handler 单测** `tests/test_bot_handlers.py`：start/archive/history/menu，覆盖审批流、所有权拒门、URL 校验、取消回调）+ E2E 脚本 + **验收清单 docs/03-acceptance.md**；§61 真实 token 联调待做 | a8d88ef+ · 0124b95 |
| M9 部署与文档 | 🔶 大部分 | README/AGENTS/架构文档齐；**生产部署手册 docs/04-deployment.md 已写**（compose 生产化+PostgreSQL 切换+备份/日志/SSRF/安全清单）；真实 VPS 15 分钟演练未做 | 9de57a4 + 7c00f2b |

**下一步优先级**（详见各里程碑小节的验收标准）：
1. 真实 `TELEGRAM_BOT_TOKEN` 的 `docker compose up --build` 交付联调（打通 §13 全链路，含视频 send_video）
2. Cookie Profile 登录网站、TikTok 视频适配（ArchiveBOT 尚无 tiktok service）、其余 Phase 2 平台

---

## M0 项目基础设施（P0）

- [ ] 初始化 git 仓库（含 git flow 分支约定：main / develop / feature-*）
- [ ] 建立目录结构（见 `02-architecture.md` §目录）
- [ ] `pyproject.toml` / `requirements.txt`：依赖锁定
- [ ] `.env.example`：全部环境变量模板（见 `02-architecture.md` §配置）
- [ ] `app/config.py`：pydantic-settings 配置加载与校验
- [ ] `Dockerfile`（Python 3.10-slim + Playwright/Chromium）、`.dockerignore`
- [ ] `docker-compose.yml`：bot / api / worker / redis / sqlite 卷
- [ ] 基础日志设施（结构化日志、级别、文件轮转）
- [ ] `Makefile` 或脚本：dev / migrate / test / lint
- [ ] 本地开发环境说明（README）

**验收**：`docker compose up` 能启动全部服务且无配置报错；`python -m app.config` 能打印有效配置。

---

## M1 数据层与国际化（P0）

- [ ] SQLAlchemy 2.0 模型（`app/database/models.py`）：
  - `users`（telegram_id 唯一、role、status、language、软删除时间戳）
  - `user_applications`（申请审批流）
  - `tasks`（url、platform、title、author、status、output_types、error、时间戳）
  - `files`（type、filename、size、local_path、telegram_file_id、deleted_at）
  - `audit_logs`（operator、action、target、details）
  - `system_settings`（key/value/updated_by）
- [ ] `app/database/database.py`：引擎、Session、连接管理（SQLite 起步，DSN 可切换 PostgreSQL）
- [ ] 迁移机制（Alembic 起步，M1 阶段先 `create_all`，M9 前引入 Alembic）
- [ ] i18n：`app/bot/i18n/locales/{zh-CN,en-US}/messages.json`
- [ ] i18n 加载器：按 `users.language`（zh-CN / en-US / auto→telegram language_code）取文案
- [ ] 枚举常量（TaskStatus、UserRole、UserStatus、FileType、OutputType、错误码）集中定义

**验收**：可创建/查询用户、任务、文件；中英文文案可切换；枚举无魔法字符串散落。

---

## M2 归档核心管道（P0）

- [ ] ArchiveBOT 集成：
  - 以 git 子模块 `vendor/ArchiveBOT` 引入（pin 版本）
  - worker 内 import `services.webpage_service / wechat_service / twitter_service / xhs_service / weibo_service / zhihu_service / reddit_service / youtube_service / bilibili_service / douyin_service` 等
  - 复用 ArchiveBOT 的 URL 解析与内容提取（不做重复实现）
- [ ] `app/archive/detector.py`：URL → 平台识别（微信、X、小红书、微博、知乎、Reddit、YouTube、Bilibili、抖音/TikTok、通用网页、其它）
- [ ] `app/archive/cleaner.py`：正文清洗（导航/广告/推荐/Cookie Banner/分享按钮/登录提示/评论区/浮窗/侧栏/脚本 UI；HTML 水印规则；图片内嵌水印不在本阶段处理）
- [ ] `app/archive/markdown.py`：清洗后 DOM → 规范化 Markdown（图片链接本地化/占位）
- [ ] `app/archive/pdf.py`：清洗后 HTML 模板 → Chromium Print-to-PDF（标题/作者/来源/时间/原文链接/封面/正文/页码/归档时间页脚）
- [ ] `app/archive/images.py`：正文图片下载 → `images/<NNN>.ext`，封面提取
- [ ] `app/archive/excerpt.py`：三行原文摘要（正文前三个有效句子，**不调用 LLM**，无幻觉）
- [ ] `app/archive/runner.py`：任务执行编排（FETCHING→PARSING→DOWNLOADING_IMAGES→GENERATING_MARKDOWN→GENERATING_PDF→产出）
- [ ] SSRF 防护前置：URL 主机解析后拒绝私有/环回/内网/云 metadata 地址
- [ ] 登录/JS 网站：Playwright + 独立 Cookie Profile（M9 或 Phase 2 落地，先留接口）

**验收**：对通用网页、微信公众号样例 URL 跑通「提取→清洗→MD→PDF→图片→摘要」；输出目录结构符合规格 §33。

---

## M3 任务系统（P0）

- [ ] Redis 队列（rq 或自研 worker）：任务入队/出队/重试
- [ ] `app/tasks/queue.py`：enqueue、delay、重试、TTL
- [ ] `app/tasks/worker.py`：消费队列，调用 archive.runner，更新 DB 状态，上报进度
- [ ] `app/tasks/manager.py`：任务 CRUD、取消（cancellation flag 检查点）、并发限制（单用户 2 / 全局 4，可配置）
- [ ] 状态机：QUEUED→FETCHING→PARSING→DOWNLOADING_IMAGES→GENERATING_MARKDOWN→GENERATING_PDF→UPLOADING→COMPLETED；失败 FAILED / 取消 CANCELLED（规格 §8）
- [ ] 进度事件：worker → bot 状态消息更新（轮询/推送）
- [ ] 超时与失败分类（URL 无效、404、403、超时、空内容、图片失败、PDF 失败、上传失败、存储不足）
- [ ] Telegram 上传：sendDocument（PDF/MD/zip）→ 成功后保存 `telegram_file_id` → 本地文件标记可删

**验收**：端到端一条任务从入队到完成、失败重试、取消；DB 状态全程正确。

---

## M4 Telegram 用户端（P0）

- [ ] `/start`：创建用户/申请（PENDING），双语欢迎文案（规格 §4、§22）
- [ ] 主菜单（规格 §5）：新建下载 / 下载历史 / 搜索历史 / 我的统计 / 设置 / 帮助 /（管理员）管理中心
- [ ] URL 下载流程（规格 §6）：识别平台 + 预览元数据（平台/标题）→ 格式选择键盘（PDF/Markdown/图片/全部）→ 创建任务
- [ ] 任务状态消息（规格 §8）：任务 ID、平台、状态、[取消] 按钮
- [ ] 完成消息（规格 §9）：标题、来源、作者、发布时间、3 行原文、原文链接、已生成文件清单 → 自动发送文件
- [ ] 失败消息（规格 §36）：原因/平台/URL + [重试][打开原文]
- [ ] 历史列表 + 分页（规格 §15）、历史详情（规格 §16）、按钮：获取 PDF/MD/图片/全部、打开原文、重新抓取、删除记录
- [ ] 「获取文件」vs「重新抓取」分离（规格 §17）：获取文件只走 telegram_file_id，不访问原站；重新抓取创建新 task
- [ ] 历史搜索（规格 §18）：标题/URL/平台/作者/关键词
- [ ] 我的统计（规格 §19）：时间维 + 平台维 + 格式维汇总
- [ ] 设置（规格 §20）：默认格式、摘要模式、PDF 选项、语言
- [ ] 对话隔离：所有查询 `current_user_id == task.user_id`，否则 ACCESS_DENIED（规格 §29）

**验收**：按规格 §61 的 Telegram 验收条目逐条过（中英文、URL 后选格式、状态、标题、3 行内容、原文链接、文件自动发送、历史分页搜索、重新发送、重新抓取）。

---

## M5 存储管理（P0）

- [ ] 临时文件池：`/storage/tasks/<task_uuid>/`（metadata.json、article.md、article.pdf、cover.jpg、images/）
- [ ] 用量统计（软限 800MB / 硬限 1GB / 清理目标 200MB，全部可配置）
- [ ] `app/storage/manager.py`：申请/释放/统计/删除
- [ ] `app/storage/cleanup.py`：清理策略（规格 §32）——优先删已上传 Telegram 的、最旧的、最少访问的、失败/取消任务的；禁止删 PROCESSING/UPLOADING 文件
- [ ] 达到软限后台清理；达到硬限禁止新大任务→立即清理→恢复
- [ ] 任务完成后本地文件可删除，DB 保留 telegram_file_id（历史与本地文件分离，规格 §13/§14/§37）

**验收**：模拟塞满 1GB 时触发硬限清理并恢复到 ~200MB；运行中任务文件不被误删；上传成功后清缓存不破坏历史获取。

---

## M6 管理后台（P0）

- [ ] RBAC 角色：USER / ADMIN / SUPER_ADMIN（权限矩阵见规格 §23）
- [ ] 管理员白名单引导：首个超级管理员通过 `ADMIN_IDS` 提升
- [ ] Bot 管理中心（规格 §24-§27）：用户管理、待审核、任务管理、文件管理、系统状态、日志、系统设置
- [ ] 审核流：批准/拒绝/禁用/恢复/删除（含二次确认与审计）
- [ ] Web Admin（FastAPI + Jinja2 + HTMX，规格 §41-§44）：Dashboard / Users / Pending / Tasks / Files / Logs / Settings
- [ ] Web 登录：Session 认证、强密码、CSRF、登录审计（规格 §50）
- [ ] 审计日志落库（规格 §39/§40）：User Log / Task Log / File Log / Admin Log 事件常量
- [ ] 系统状态 API：CPU/RAM/存储/队列/用户/今日任务（规格 §38）

**验收**：管理员可在 Bot 完成日常审核与查看；Web 可完成完整管理；所有管理操作有审计记录。

---

## M7 安全加固（P0）

- [ ] SSRF 防护（规格 §50）：拒绝 127.0.0.1 / localhost / 内网 / Docker 内网 / 云 metadata endpoint
- [ ] callback 安全（规格 §30）：解析→查库→验证用户身份→验证任务所有权→验证状态→验证文件权限→RBAC，再执行
- [ ] 越权防护：file_id 只发给所属用户；历史/详情查询强制 ownership check
- [ ] 防重复任务、URL 数量限制、并发限制、文件大小限制（服务端校验，不信任 callback）
- [ ] Web Admin：HTTPS、Session/JWT、强密码、CSRF、Rate Limit
- [ ] 管理员操作均服务端再次验证（不依赖客户端状态）

**验收**：安全测试清单（恶意 callback、越权访问他人 file_id、内网 SSRF 探测）全部拦截。

---

## M8 测试与验收（P0）

- [ ] 单元测试：detector、cleaner、excerpt、storage cleanup、config、i18n
- [ ] 数据库测试：模型 CRUD、ownership 查询
- [ ] 集成测试：队列→worker→DB 状态流转（mock 抓取与 Telegram 上传）
- [ ] Bot handler 测试（aiogram 测试框架 / mock Update）
- [ ] 安全测试：SSRF、callback 越权（见 M7）
- [ ] 按规格 §61 验收清单逐条核验并记录结果
- [ ] CI：lint（ruff）+ 测试（pytest）+ 构建

**验收**：`pytest` 全绿；验收清单文档化。

---

## M9 部署与文档（P0）

- [ ] docker-compose 生产配置（卷、健康检查、重启策略、日志轮转）
- [ ] `.env.example` 完整注释；部署手册（VPS 建议见规格 §52）
- [ ] Alembic 迁移落地（SQLite→PostgreSQL 可切换）
- [ ] Playwright/Chromium 在容器内的安装与缓存
- [ ] 运维手册：日志、监控、备份（DB 与 redis）、升级
- [ ] README：功能、快速开始、目录说明、开发指南

**验收**：全新 VPS 按文档 15 分钟内跑通 MVP。

---

## Phase 2（后续）

批量 URL（§55）、Telegram Forward（§56）、AI Summary 选项（§10/§20）、平台/时间统计增强、标签、高级搜索、PDF 模板选择、图片压缩、多 Worker、PostgreSQL 生产化、对象存储可选、登录网站 Cookie Profile、更多平台适配。

## Phase 3（后续）

Web 用户端、API + API Key、Webhook、RSS、浏览器扩展、Obsidian/Zotero 集成、RAG/AI 知识库、多语言扩展、多 Bot、多租户。

---

## 开发顺序（已按此执行完毕，留作追溯）

1. M0：脚手架 + 配置 + Docker（先跑通）
2. M1：数据层 + i18n
3. M2/M3：归档管道与任务系统骨架（先接 ArchiveBOT 通用网页 + 微信公众号）
4. M4：Telegram 用户端核心链路（/start → URL → 格式 → 任务 → 完成消息）
5. 随后按优先级补 M5-M9。

后续新任务从「0.1 状态表」的**下一步优先级**领取；多 Agent 协作规则见仓库根 [AGENTS.md](../AGENTS.md)。
