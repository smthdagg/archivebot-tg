#!/usr/bin/env bash
# 将本地 ArchiveBOT-TG 代码同步部署到 VPS（/opt/archivebot）
# 用法: ./scripts/deploy-to-vps.sh [--skip-tests] [--skip-build] [--no-restart]
#   --skip-tests    跳过本地 pytest/ruff 门禁（不推荐，仅紧急修复用）
#   --skip-build    跳过 docker compose build（仅代码/模板改动且未新增依赖时可用）
#   --no-restart    只上传解压，不重建容器（需手动 docker compose up -d --build）
#
# 前置: 本机已配好 VPS SSH（见 docs/07-vps-deployment.md §1）
#       项目根目录执行本脚本

set -euo pipefail

# ---------- 可配置项（按需修改） ----------
# 私有连接参数从 scripts/deploy.env.local（gitignored）读取，不硬编码在仓库里
ENV_LOCAL="$(cd "$(dirname "$0")" && pwd)/deploy.env.local"
[ -f "$ENV_LOCAL" ] && source "$ENV_LOCAL"
VPS_HOST="${VPS_HOST:?set VPS_HOST in scripts/deploy.env.local (gitignored) or env}"
VPS_PORT="${VPS_PORT:-22}"                        # SSH 端口
VPS_USER="${VPS_USER:-root}"                      # SSH 用户
VPS_DIR="${VPS_DIR:-/opt/archivebot}"             # VPS 部署目录
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"        # SSH 密钥路径；私有部署在 scripts/deploy.env.local 里覆盖
# 需要排除的本地目录/文件（打包时跳过）
# ⚠️ .env 必须排除：VPS 部署目录的 .env 是生产配置（含并发/密钥），
#    绝不随代码覆盖（否则 deploy 会丢掉 VPS 上的独立配置）。
EXCLUDES=(.git .gitmodules .gitignore .venv .pytest_cache .ruff_cache .worktrees .serena .zcode
          __pycache__ "*.pyc" .DS_Store storage data .env .env.example)
# -----------------------------------------

SKIP_TESTS=0; SKIP_BUILD=0; NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --no-restart) NO_RESTART=1 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

cd "$(dirname "$0")/.."   # 项目根

echo "==> [1/5] 本地质量门禁"
if [ "$SKIP_TESTS" = "1" ]; then
  echo "    (跳过 tests/ruff)"
else
  .venv/bin/python -m pytest -q -m "not slow" 2>/dev/null || { echo "pytest 失败，中止"; exit 1; }
  .venv/bin/ruff check app/ tests/ scripts/ migrations/ 2>/dev/null || { echo "ruff 失败，中止"; exit 1; }
fi

echo "==> [2/5] 打包（排除: ${EXCLUDES[*]}）"
EXCL_ARGS=()
for e in "${EXCLUDES[@]}"; do EXCL_ARGS+=(--exclude="$e"); done
PKG="/tmp/archivebot-deploy-$(date +%s).tar.gz"
tar "${EXCL_ARGS[@]}" -czf "$PKG" .
echo "    包: $PKG ($(du -h "$PKG" | cut -f1))"

echo "==> [3/5] 上传并解压到 $VPS_USER@$VPS_HOST:$VPS_DIR"
SSH_OPTS=(-p "$VPS_PORT" -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
if [ -n "$SSH_KEY" ]; then SSH_OPTS+=(-i "$SSH_KEY"); fi
cat "$PKG" | ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" \
  "mkdir -p '$VPS_DIR' && cat > '$VPS_DIR/pkg.tgz' && cd '$VPS_DIR' && tar xzf pkg.tgz && rm -f pkg.tgz && echo '    解压完成'"
rm -f "$PKG"

echo "==> [4/5] 重建并重启容器"
if [ "$SKIP_BUILD" = "1" ]; then
  echo "    (跳过 build)"
else
  ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" "cd '$VPS_DIR' && docker compose build 2>&1 | tail -3"
fi

if [ "$NO_RESTART" = "1" ]; then
  echo "    (--no-restart: 未重启，请手动执行 cd $VPS_DIR && docker compose up -d --build)"
else
  ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" "cd '$VPS_DIR' && docker compose up -d 2>&1 | tail -5"
fi

echo "==> [5/5] 验证"
ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" \
  "cd '$VPS_DIR' && docker compose ps --format '  {{.Name}}: {{.Status}}' | sed 's/^/  /' && \
   echo '  healthz: ' && curl -s http://127.0.0.1:8080/healthz || true"

echo "==> 部署完成 ✅"
echo "    如涉及数据库模型变更，请先在 VPS 执行:"
echo "    cd $VPS_DIR && docker compose run --rm --no-deps api sh -c 'alembic upgrade head'"