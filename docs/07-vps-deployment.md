# ArchiveBOT-TG VPS 实际部署记录（Doc 07）

> 最后更新：2026-09-03
> 本文件记录 **当前实际部署实例** 的具体信息（服务器、路径、容器、访问方式、运维命令）。
> 通用部署步骤见 [docs/04-deployment.md](04-deployment.md)。
> **代码修改后部署到 VPS：见 §8（一键脚本 `scripts/deploy-to-vps.sh`）。**

---

## 1. 部署概览

| 项目 | 值 |
|---|---|
| 服务器 | `henry.x.com`（香港 VPS，Debian 13，2 vCPU / 1.9G RAM / 15G 磁盘） |
| IPv4 / IPv6 | `<VPS-IPv4>` / `<VPS-IPv6>` |
| SSH | 端口 `22`（标准）/ `2222`（备用），root + 密钥登录 |
| 部署目录 | `/opt/archivebot/` |
| 数据库方案 | **SQLite**（`data/archivebot.db`，单文件，随 compose bind mount） |
| Web Admin | FastAPI `:8080`，**仅内网**（外网已被封锁，走 SSH 隧道访问） |
| Telegram Bot | `@ArichivePDF_bot`（长轮询，无需公网入站） |

---

## 2. 容器组成

```bash
cd /opt/archivebot && docker compose ps
```

| 容器 | 镜像 | 入口 | 状态策略 |
|---|---|---|---|
| `archivebot-bot-1` | archivebot-bot | `python -m app.bot.main` | restart: unless-stopped |
| `archivebot-api-1` | archivebot-api | `python -m app.main`（uvicorn :8080） | 同上 |
| `archivebot-worker-1` | archivebot-worker | `python -m app.tasks.worker`（rq） | 同上 |
| `archivebot-redis-1` | redis:8-alpine | 官方入口 | 同上 |

- 数据：`/opt/archivebot/data/archivebot.db`（SQLite）+ `/opt/archivebot/storage/`（任务产物）
- 队列：redis 命名卷 `archivebot_redis-data`（可丢，无持久业务数据）

---

## 3. 部署方式（本次执行步骤）

本地项目已推送子模块完整内容，直接打包上传：

```bash
# 本地打包（排除 git/venv/缓存）
cd ~/Documents/ArchiveBot  # 项目根
tar --exclude=".git" --exclude=".venv" --exclude=".pytest_cache" \
    --exclude=".ruff_cache" --exclude=".worktrees" --exclude=".serena" \
    --exclude=".zcode" --exclude="__pycache__" --exclude="*.pyc" \
    -czf /tmp/archivebot-deploy.tar.gz .

# 上传解压
scp -P 2222 /tmp/archivebot-deploy.tar.gz root@<VPS-IPv4>:/opt/archivebot/
ssh -p 2222 root@<VPS-IPv4> 'cd /opt/archivebot && tar xzf archivebot-deploy.tar.gz && rm -f archivebot-deploy.tar.gz'

# 构建启动
ssh -p 2222 root@<VPS-IPv4> 'cd /opt/archivebot && docker compose up -d --build'
```

> ⚠️ 上传包含 `.env`（含 Telegram token 等密钥），**勿外传仓库**；本地 `.env` 已含生产所需配置（token、ADMIN_IDS、Web Admin 密码等）。

---

## 4. 访问方式

### 4.1 Telegram Bot

直接私聊 `@ArichivePDF_bot`。发链接 → 选格式（PDF / Markdown / 长截图）→ 收文件。功能与本地实例一致。

### 4.2 Web Admin（SSH 隧道）

8080 端口外网已被封锁（见 §6），通过 SSH 隧道访问：

```bash
# 建立隧道（2222 或 22 端口均可）
ssh -L 8080:127.0.0.1:8080 -p 2222 -i ~/.ssh/your_key root@<VPS-IPv4>

# 浏览器访问（保持隧道会话开启）
open http://127.0.0.1:8080/admin
```

- 登录密码：`WEB_ADMIN_PASSWORD`（见 `/opt/archivebot/.env`）
- 健康检查：`http://127.0.0.1:8080/healthz` → `{"ok":true}`

---

## 5. 日常运维

```bash
cd /opt/archivebot

docker compose ps              # 状态
docker compose logs -f bot     # bot 日志
docker compose logs -f worker  # worker 日志
docker compose logs -f api     # Web Admin 日志
docker compose restart bot     # 重启单个服务
docker compose up -d           # 应用 .env/配置变更后重建
```

**数据备份**（SQLite）：

```bash
# 方式一：停机冷备
docker compose stop bot api worker
cp /opt/archivebot/data/archivebot.db /opt/archivebot/data/archivebot.db.bak-$(date +%F)
docker compose up -d

# 方式二：sqlite 在线备份（API 容器内）
docker compose exec api sh -c 'sqlite3 /app/data/archivebot.db ".backup /backup/archivebot-$(date +%F).db"'
```

**升级**：

```bash
# 重新从本地打包上传（或在服务器上 git pull）
docker compose build
docker compose stop bot api worker
docker compose run --rm --no-deps api sh -c "alembic upgrade head"
docker compose up -d
```

---

## 6. 安全与已知事项

### 6.1 Web Admin 外网封锁（DOCKER-USER）

Docker 的端口映射会绕过 UFW（iptables DNAT 优先级更高），8080 曾可被外网直连。
已通过 **DOCKER-USER 链** 拦截并持久化：

```bash
# systemd 服务：/etc/systemd/system/block-8080.service
# 开机自动执行：iptables -I DOCKER-USER -p tcp --dport 8080 -j DROP
systemctl status block-8080     # 应 active
iptables -L DOCKER-USER -n      # 应看到 8080 DROP
```

验证：外网 `curl http://<VPS-IPv4>:8080/healthz` 超时；服务器本机 `curl http://127.0.0.1:8080/healthz` 返回 200。

### 6.2 Telegram 单实例约束

同一 bot token 只允许一个 getUpdates 长轮询。**本地与 VPS 不可同时运行 bot**（会报 `TelegramConflictError`）。
当前：本地实例已停，VPS 独占运行。

### 6.3 迁移与本地数据

- 本地 `data/archivebot.db`（200KB）与 `storage/`（27 任务 ≈ 13M）已随部署同步至 VPS `/opt/archivebot/`
- 本地 Docker 中 ArchiveBot 容器已停止（`docker compose down`），数据保留在本地项目目录，可随时回迁

### 6.4 服务器共存服务

同一 VPS 还运行着其他服务（互不影响，端口均不冲突）：

| 服务 | 端口 | 说明 |
|---|---|---|
| x-ui 面板 / 订阅 | 41084（内网）/ 2096 | 代理服务（Wawo-HK-VPS） |
| Xray 节点 | 443 / 46677 / 46678 | VLESS+Reality |
| SSH | 22 / 2222 | 密钥登录 |
| tg-ytdlp-bot（Docker） | 80 / 8443 / 5555 | YouTube 下载机器人 |

---

## 8. 代码修改后部署到 VPS（必读）

> **每次修改代码并提交后，都需要执行本流程，让 VPS 上的实例与代码同步。**
> 一键脚本：`scripts/deploy-to-vps.sh`（本地项目根目录执行）。

### 8.1 一键部署（推荐）

```bash
# 项目根目录
./scripts/deploy-to-vps.sh          # 完整流程：测试→打包→上传→构建→重启→验证
./scripts/deploy-to-vps.sh --skip-tests   # 跳过 pytest/ruff（紧急修复）
./scripts/deploy-to-vps.sh --skip-build   # 跳过 docker build（仅改模板/代码，未加依赖）
```

脚本步骤（5 步）：
1. **本地质量门禁**：`pytest -m "not slow"` + `ruff check`（失败中止）
2. **打包**：排除 `.git/.venv/缓存/storage/data` 等，生成 tar.gz
3. **上传解压**：到 `$VPS_USER@$VPS_HOST:$VPS_DIR`（默认 `root@<VPS-IPv4>:2222 → /opt/archivebot`，可用环境变量 `VPS_HOST/VPS_PORT/VPS_DIR/SSH_KEY` 覆盖）
4. **重建重启**：`docker compose build && docker compose up -d`
5. **验证**：容器状态 + `curl /healthz`

> ⚠️ 打包**不包含** `.env`（在排除列表外？——不，`.env` 被显式排除？见下）。**`.env` 永不入库**：部署目录的 `.env` 是 VPS 上的既有文件，打包解压不会覆盖它（见脚本 EXCLUDES 与 §6.2 说明，若需更新 .env 请直接在 VPS 上编辑）。

### 8.2 手动流程（脚本不可用时逐条执行）

```bash
# 1) 质量门禁
.venv/bin/python -m pytest -q -m "not slow"
.venv/bin/ruff check app/ tests/ scripts/ migrations/

# 2) 打包上传
tar --exclude=".git" --exclude=".venv" --exclude=".pytest_cache" \
    --exclude=".ruff_cache" --exclude=".worktrees" --exclude=".serena" \
    --exclude=".zcode" --exclude="__pycache__" --exclude="*.pyc" \
    --exclude="data" --exclude="storage" \
    -czf /tmp/ab.tgz .
scp -P 2222 /tmp/ab.tgz root@<VPS-IPv4>:/opt/archivebot/
ssh -p 2222 root@<VPS-IPv4> 'cd /opt/archivebot && tar xzf ab.tgz && rm -f ab.tgz'

# 3) 重建重启
ssh -p 2222 root@<VPS-IPv4> 'cd /opt/archivebot && docker compose up -d --build'

# 4) 验证
ssh -p 2222 root@<VPS-IPv4> 'cd /opt/archivebot && docker compose ps && curl -s http://127.0.0.1:8080/healthz'
```

### 8.3 数据库模型变更（涉及 Alembic）

改数据模型后，除代码部署外还必须执行迁移：

```bash
# 本地生成迁移
DATABASE_URL=sqlite:///data/archivebot.db .venv/bin/alembic revision --autogenerate -m "描述"
# 部署代码后，在 VPS 执行迁移
ssh -p 2222 root@<VPS-IPv4> \
  'cd /opt/archivebot && docker compose run --rm --no-deps api sh -c "alembic upgrade head"'
```

### 8.4 部署检查清单

- [ ] pytest + ruff 全绿
- [ ] 涉及 schema：迁移文件已生成并提交
- [ ] `scripts/deploy-to-vps.sh` 执行成功（或手动流程完成）
- [ ] `docker compose ps` 四个容器 Up
- [ ] `curl http://127.0.0.1:8080/healthz` 返回 `{"ok":true}`
- [ ] Telegram bot 可正常对话（功能冒烟）

---

## 7. 相关文档

- 通用部署指南：[docs/04-deployment.md](04-deployment.md)
- 架构设计：[docs/02-architecture.md](02-architecture.md)
- 快速开始：[README.md](../README.md)
---

## 9. 运维组件（2026-09-04 起，与项目日志协同）

### 9.1 代码部署契约
- **修改代码后必须用 `scripts/deploy-to-vps.sh` 部署**（不再手动 scp 单文件）。
- **`.env` 已加入排除清单**（`9ea90ce`）：VPS 部署目录的 `.env` 是生产配置（并发/密钥），
  永不被代码部署覆盖；改配置直接在 VPS 编辑 `/opt/archivebot/.env` 后 `docker compose up -d`。
- IPv6 连接：`VPS_HOST=<VPS-IPv6> VPS_PORT=22 ./scripts/deploy-to-vps.sh`

### 9.2 定时运维（VPS cron，均已在 /root/ops 手册登记）
| cron | 组件 | 职责 |
|---|---|---|
| 每日 03:10 | `ops-archivebot-backup.sh` | SQLite 在线一致性备份 + storage tar → `/root/ops/backups/archivebot/`（DB 14 份 / storage 7 份） |
| 每 5 分钟 | `ops-archivebot-health.py` | 4 容器 + healthz + bot 轮询活动检查；异常/恢复经 tg-ytdlp bot 发 Telegram |
| 每日 23:55 | `ops-daily-snapshot` | 服务/容器/端口/资源快照 |
| 每周日 03:30 | `ops-weekly-cleanup` v2 | 与 ArchiveBot 解冲突（去 volume prune、tmp 保护、drop_caches 阈值、目录保护） |
| 每日 18:29 | acme.sh | 订阅证书续期（80 端口保持空闲） |

### 9.3 生产配置（VPS `.env` 独立）
- `MAX_GLOBAL_CONCURRENCY=3` / `MAX_USER_CONCURRENCY=2`（1.9G 内存；曾降至 2/1 引发“僵尸任务占槽→并发限流事故”，已加 `reap_stale_tasks` 自愈 + `CONCURRENCY_LIMIT` 友好错误，见 `aae3ff4`）
- swap：系统默认 + 2G `/swapfile2`（fstab 持久化）
- 容器日志轮转：json-file `20m × 3`（compose `x-logging`，`dd9ef4c`）
