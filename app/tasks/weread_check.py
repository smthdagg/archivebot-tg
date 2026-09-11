"""公众号订阅增量检查与分发（微信读书路线，Phase 2）。

worker 侧定时任务：遍历 active 公众号账号 → weread 桥接增量拉文章列表（synckey
游标）→ 按订阅者的交付方式分发：

- notify（默认）：登记 WxPendingArticle 后推「标题 + 链接 + [归档] 按钮」，
  用户点击才建任务；
- auto：直接 create_task（platform=wechat，复用现有直连管道）并入队交付文件。

游标语义：账号级 synckey 是微信读书增量刷新 token（非页码），拉取成功即回存；
订阅者级 last_article_time 是分发基线（订阅时刻起效，不分发历史文章）。分发
失败（容量满/配额满）不推进该文章的基线，下轮补齐。付费文章（payType=2）不
分发（规格红线：不绕过访问控制）。

单 worker 假设：本模块不做跨进程互斥，worker 副本 >1 时需先加分布式锁。
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.archive import weread_client
from app.archive.weread_client import WereadError, WereadTokenExpired, WereadVerifyNeeded
from app.config import get_settings
from app.database.database import SessionLocal
from app.database.enums import AuditAction, DeliveryMode, OutputType, Platform, TaskStatus
from app.database.models import SystemSetting, Task, User, WxMpAccount, WxPendingArticle, WxSubscription
from app.database.services import audit
from app.tasks import manager as task_manager
from app.tasks.manager import TaskLimitError
from app.tasks.queue import enqueue_task

logger = logging.getLogger(__name__)

_REMINDER_COOLDOWN_SECONDS = 24 * 3600  # 管理员提醒冷却（同 cookie_expiry 模式）
_PAGE_COUNT = 20  # 每账号每轮拉取的文章条数（微信读书增量页大小）


def _cleanup_stale_pendings(db) -> int:
    """清理超过 7 天的通知按钮登记（用户不点击的旧文不再可归档）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    deleted = db.query(WxPendingArticle).filter(WxPendingArticle.created_at < cutoff).delete()
    if deleted:
        db.commit()
        logger.info("cleaned %d stale wx pending articles", deleted)
    return int(deleted or 0)


# ---------------------------------------------------------------------------
# 提醒冷却（SystemSetting 时间戳，与 cookie_expiry 同模式）
# ---------------------------------------------------------------------------

def _reminder_ok(kind: str) -> bool:
    db = SessionLocal()
    try:
        key = f"weread_reminder_last:{kind}"
        row = db.get(SystemSetting, key)
        if row is None or not row.value:
            return True
        try:
            last = float(row.value)
        except ValueError:
            return True
        return time.time() - last > _REMINDER_COOLDOWN_SECONDS
    finally:
        db.close()


def _mark_reminded(kind: str) -> None:
    db = SessionLocal()
    try:
        key = f"weread_reminder_last:{kind}"
        row = db.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key)
        row.value = str(time.time())
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def notify_admins(kind: str, text: str) -> None:
    """向 ADMIN_IDS 推 Telegram 提醒（24h 冷却，按 kind 区分）。"""
    settings = get_settings()
    if not settings.admin_ids or not _reminder_ok(kind):
        return
    _mark_reminded(kind)
    from app.bot.delivery import run_async
    from app.bot.delivery import send_message as _send

    for admin_id in settings.admin_ids:
        try:
            run_async(_send(admin_id, text))
        except Exception as e:  # noqa: BLE001
            logger.warning("weread reminder to %s failed: %s", admin_id, e)


# ---------------------------------------------------------------------------
# wechat cookie profile 自动关联（与 archive.py 的登录类平台逻辑一致）
# ---------------------------------------------------------------------------

def auto_wechat_profile() -> str | None:
    """从 cookie_profiles_file 里找配置了 wechat 平台 cookie 的 profile 名。"""
    import json
    from pathlib import Path

    settings = get_settings()
    path_str = settings.cookie_profiles_file
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_absolute():
        # 容器内 cwd=/app 时相对路径直接命中；兜底 /app 前缀
        for base in (Path.cwd(), Path("/app")):
            candidate = base / path
            if candidate.exists():
                path = candidate
                break
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    for name, platforms in data.items():
        if isinstance(platforms, dict) and platforms.get(Platform.WECHAT.value):
            return name
    return None


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

def _user_at_capacity(db, user_id: int) -> bool:
    settings = get_settings()
    task_manager.reap_stale_tasks(db, settings.task_timeout_seconds)
    if task_manager.user_active_task_count(db, user_id) >= settings.max_user_concurrency:
        return True
    return task_manager.global_active_task_count(db) >= settings.max_global_concurrency


def _has_existing_task(db, user_id: int, url: str) -> bool:
    row = db.scalar(
        select(Task.id).where(
            Task.user_id == user_id,
            Task.url == url,
            Task.status.notin_((TaskStatus.FAILED.value, TaskStatus.CANCELLED.value)),
        ).limit(1)
    )
    return row is not None


async def deliver_notification_async(db, sub: WxSubscription, article: dict) -> bool:
    """notify 交付（异步核心）：登记 pending + 推按钮消息。成功 True（推进基线）。

    bot 进程内直接 ``await``；worker 进程经 ``_notify_subscriber()`` 包装。
    """
    pending = WxPendingArticle(
        account_id=sub.account_id,
        doc_url=article["doc_url"],
        title=article.get("title") or None,
        article_time=article.get("article_time") or 0,
    )
    db.add(pending)
    db.flush()  # 拿 pending.id
    from app.bot.delivery import send_message as _send
    from app.bot.i18n import t
    from app.bot.keyboards import wx_archive_button

    user = db.get(User, sub.user_id)
    lang = user.language if user and user.language in ("zh-CN", "en-US") else "zh-CN"
    text = t(
        lang,
        "subscribe.new_article",
        title=article.get("title") or "-",
        url=article["doc_url"],
    )
    try:
        await _send(sub.chat_id, text, reply_markup=wx_archive_button(lang, pending.id))
    except Exception as e:  # noqa: BLE001
        logger.warning("notify push to chat %s failed: %s", sub.chat_id, e)
        db.rollback()
        return False
    return True


def _notify_subscriber(db, sub: WxSubscription, article: dict) -> bool:
    """worker 侧同步入口（持久事件循环上执行异步交付）。"""
    from app.bot.delivery import run_async

    return run_async(deliver_notification_async(db, sub, article))


async def deliver_auto_archive_async(db, sub: WxSubscription, article: dict, cookie_profile: str | None) -> bool:
    """auto 交付（异步核心）：直接建归档任务并入队。成功 True（推进基线）。"""
    return _auto_archive_subscriber(db, sub, article, cookie_profile)


def _auto_archive_subscriber(db, sub: WxSubscription, article: dict, cookie_profile: str | None) -> bool:
    """auto 模式：直接建归档任务并入队。成功返回 True（推进基线）。"""
    if _user_at_capacity(db, sub.user_id):
        return False
    if _has_existing_task(db, sub.user_id, article["doc_url"]):
        # 已归档过同文：视为分发完成，推进基线避免每轮重复
        return True
    output_types = sub.output_types or [OutputType.PDF.value]
    try:
        task = task_manager.create_task(
            db,
            user_id=sub.user_id,
            chat_id=sub.chat_id,
            url=article["doc_url"],
            platform=Platform.WECHAT.value,
            output_types=list(output_types),
            cookie_profile=cookie_profile,
        )
        audit(
            db,
            action=AuditAction.TASK_CREATED,
            operator_user_id=sub.user_id,
            target_type="task",
            target_id=task.id,
            details={"source": "subscription", "account_id": sub.account_id},
        )
        db.commit()
    except TaskLimitError as e:
        logger.info("subscription task for chat %s rejected: %s", sub.chat_id, e.code)
        db.rollback()
        return False
    except Exception:  # noqa: BLE001
        logger.exception("subscription task creation failed for chat %s", sub.chat_id)
        db.rollback()
        return False
    enqueue_task(task.id)
    return True


async def backfill_recent_articles(db, sub: WxSubscription, count: int, cookie_profile: str | None) -> int:
    """订阅时补拉最近 N 篇（offset 翻页，最新→旧，可回放）。

    与增量流的区别：offset 翻页不消费服务端游标，可反复读；基线过滤仍生效
    （同文不重复交付），交付成功则把基线推进到该文时间。返回交付篇数。
    """
    if count <= 0:
        return 0
    try:
        page = await weread_client.call_async(
            "articles", sub.account_id, "--count", "20", "--offset", "0"
        )
    except WereadError as e:
        logger.warning("backfill fetch failed for %s: %s", sub.account_id, e)
        return 0
    articles = (page.get("articles") or [])[:count]
    # 先过滤（基线内已交付/付费/无时间戳），再按时间升序交付（阅读顺序旧→新），
    # 避免先推最新篇把基线推过更旧的同批文章
    eligible = sorted(
        (a for a in articles
         if (a.get("article_time") or 0) > (sub.last_article_time or 0)
         and (a.get("article_time") or 0) > 0
         and a.get("pay_type") != 2),
        key=lambda a: a["article_time"],
    )
    delivered = 0
    for article in eligible:
        if sub.delivery_mode == DeliveryMode.AUTO.value:
            ok = await deliver_auto_archive_async(db, sub, article, cookie_profile)
        else:
            ok = await deliver_notification_async(db, sub, article)
        if ok:
            delivered += 1
    if delivered and eligible:
        newest = max(a["article_time"] for a in eligible[:count])
        sub.last_article_time = max(sub.last_article_time or 0, newest)
        db.commit()
    logger.info("backfill for %s/%s: %d delivered", sub.account_id, sub.id, delivered)
    return delivered


def _fan_out_account(db, account: WxMpAccount, page: dict, cookie_profile: str | None, stats: dict) -> None:
    settings = get_settings()
    articles = page.get("articles", [])
    cap = settings.wasub_max_articles_per_cycle
    if len(articles) > cap:
        logger.warning(
            "account %s returned %d articles, processing first %d (rest dropped this cycle)",
            account.account_id, len(articles), cap,
        )
        articles = articles[:cap]

    subs = db.scalars(
        select(WxSubscription).where(
            WxSubscription.account_id == account.account_id,
            WxSubscription.active.is_(True),
        )
    ).all()
    if not subs:
        return

    for article in articles:
        article_time = article.get("article_time") or 0
        if article_time <= 0:
            continue  # 无时间戳无法定基线，跳过
        if article.get("pay_type") == 2:
            continue  # 付费内容不分发（红线 10）
        for sub in subs:
            if article_time <= (sub.last_article_time or 0):
                continue
            if sub.delivery_mode == DeliveryMode.AUTO.value:
                delivered = _auto_archive_subscriber(db, sub, article, cookie_profile)
                if delivered:
                    stats["auto"] += 1
            else:
                delivered = _notify_subscriber(db, sub, article)
                if delivered:
                    stats["notify"] += 1
            if delivered:
                sub.last_article_time = max(sub.last_article_time or 0, article_time)
                db.commit()

    account.last_checked_at = datetime.now(timezone.utc)
    db.commit()


def run_subscription_check(db=None) -> dict:
    """增量检查所有 active 账号并分发新文章。worker 定时线程调用。

    返回统计 dict（accounts/notify/auto/errors），仅用于日志。
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        stats = {"accounts": 0, "notify": 0, "auto": 0, "errors": 0}
        accounts = db.scalars(select(WxMpAccount).where(WxMpAccount.active.is_(True))).all()
        if not accounts:
            return stats
        if not weread_client.node_available():
            logger.warning("weread bridge unavailable (no node runtime), skipping cycle")
            return stats

        cookie_profile = auto_wechat_profile()
        token_failed: WereadError | None = None

        try:
            _cleanup_stale_pendings(db)
        except Exception:  # noqa: BLE001 - 清理失败不阻断本轮分发
            logger.exception("stale pending cleanup failed")
            db.rollback()

        for account in accounts:
            if token_failed:
                break  # 登录态问题是账号无关的，整轮暂停
            stats["accounts"] += 1
            opts = ["--count", str(_PAGE_COUNT)]
            if account.last_synckey:
                opts += ["--synckey", str(account.last_synckey)]
            else:
                # 首拉显式传 synckey=0（上游文档语义：0=全新拉取）。
                # 注意该接口是读后即消费的增量流：服务端游标前移后，传旧
                # synckey 也不回放历史——漏掉的文章靠订阅基线兜底（不推进）。
                opts += ["--synckey", "0"]
            try:
                page = weread_client.call_sync("articles", account.account_id, *opts)
            except (WereadTokenExpired, WereadVerifyNeeded) as e:
                token_failed = e
                break
            except WereadError as e:
                logger.warning("articles fetch failed for %s: %s", account.account_id, e)
                stats["errors"] += 1
                continue

            if page.get("synckey") is not None:
                account.last_synckey = int(page["synckey"])
            try:
                _fan_out_account(db, account, page, cookie_profile, stats)
            except Exception:  # noqa: BLE001
                logger.exception("fan-out failed for %s", account.account_id)
                stats["errors"] += 1
                db.rollback()
                continue

        if token_failed is not None:
            logger.warning("weread token/verify failure: %s", token_failed)
            if isinstance(token_failed, WereadTokenExpired):
                notify_admins(
                    "token",
                    "⚠️ 微信读书登录已失效，公众号订阅暂停。\n"
                    f"{token_failed}\n"
                    "管理员请执行 /weread_login 重新扫码。",
                )
            else:
                notify_admins(
                    "verify",
                    "⚠️ 微信读书触发人工验证（-2041），公众号订阅暂停。\n"
                    f"{token_failed}\n"
                    "请在手机上打开微信读书官方 App 正常使用以完成验证"
                    "（重新扫码无法解除），稍后自动恢复。",
                )
        if any(stats[k] for k in ("accounts", "notify", "auto", "errors")):
            logger.info("weread subscription check done: %s", stats)
        return stats
    finally:
        if own_db:
            db.close()


# ---------------------------------------------------------------------------
# 定时循环（worker.py 启动的 daemon 线程）
# ---------------------------------------------------------------------------

def subscription_loop(stop_event=None) -> None:
    """立即跑一次，然后按 wasub_check_interval_minutes 循环（单 worker 假设）。"""
    while True:
        interval_minutes = max(15, get_settings().wasub_check_interval_minutes)
        started = time.monotonic()
        try:
            run_subscription_check()
        except Exception:  # noqa: BLE001 - 循环永不因单轮失败退出
            logger.exception("weread subscription check crashed")
        sleep_seconds = max(60, interval_minutes * 60 - (time.monotonic() - started))
        if stop_event is not None:
            if stop_event.wait(sleep_seconds):
                return
        else:
            time.sleep(sleep_seconds)
