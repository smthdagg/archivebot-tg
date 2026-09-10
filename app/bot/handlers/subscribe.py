"""公众号订阅（微信读书路线，Phase 2）。

/subscribe <公众号名|文章URL> → 搜索 MP_WXS_<id> → 选交付方式（notify 默认 /
auto+格式）→ 落库并发起服务端微信读书订阅。
/subscribe（无参）＝我的订阅管理（切换模式 / 退订）。
/weread_login（ADMIN_IDS）＝扫码登录服务端共享账号。
/weread_status（ADMIN_IDS）＝登录态与订阅概况。

callback 约定（≤64 字节）：
  wsubpick:{accountId}          选中候选公众号 → 交付方式选择
  wsubmode:{accountId}:{mode}   notify 直接完成；auto 转格式选择
  wsubfmt:{accountId|subId}:{fmt}  新建（auto）/改格式
  wsubchg:{subId}:{mode}        已有订阅切换交付方式
  wsubrm:{subId}                退订（服务端校验所有权）
  wsubgo:{pendingId}            通知按钮 → 交给现有格式选择 FSM 建任务
"""

import html as html_lib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy import func, select

from app.archive import weread_client
from app.archive.ssrf import validate_url
from app.archive.weread_client import (
    WereadError,
    WereadTokenExpired,
    WereadVerifyNeeded,
)
from app.bot.common import user_language
from app.bot.handlers.archive import ArchiveState, _output_types
from app.bot.i18n import t
from app.bot.keyboards import format_selector
from app.config import get_settings
from app.database.database import SessionLocal
from app.database.enums import AuditAction, DeliveryMode, Platform, UserStatus
from app.database.models import WxMpAccount, WxPendingArticle, WxSubscription
from app.database.services import audit, get_user_by_telegram_id
from app.tasks.weread_check import notify_admins

logger = logging.getLogger(__name__)

router = Router(name="subscribe")

# 通知按钮的归档入口 7 天后失效（pending 行由 weread_check 定期清理）
_PENDING_TTL = timedelta(days=7)

_ARTICLE_URL_RE = re.compile(r"https?://\S+")
# 公众号文章页里的号名（多代模板，逐个尝试；只做 best-effort 提示，失败则请用户发名称）
_NICKNAME_PATTERNS = (
    re.compile(r'var nickname = "([^"]+)"'),
    re.compile(r'"profile_nickname"\s*:\s*"([^"]+)"'),
    re.compile(r"var nickname = '([^']+)'"),
)


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids


def _ascii_qr(url: str) -> str:
    """二维码 → 半块字符画（▀▄█ 每行打包两行模块）。

    微信登录 URL 的二维码约 45×45 模块，逐模块 2×2 字符渲染会超 Telegram
    4096 字符上限（MESSAGE_TOO_LONG 导致登录流程卡死）。半块渲染后约
    1.2k 字符，且宽高比 ≈1.2:1 接近方形，可正常扫描。
    """
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:  # 奇数行补一行空白，方便两两打包
        matrix.append([False] * len(matrix[0]))
    lines = []
    for y in range(0, len(matrix), 2):
        row = []
        for x in range(len(matrix[y])):
            top, bottom = matrix[y][x], matrix[y + 1][x]
            row.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        lines.append("".join(row))
    return "\n".join(lines)


def _nickname_from_article(url: str) -> str | None:
    """从公众号文章页 HTML 提取号名（best-effort：先 SSRF 校验，禁重定向）。"""
    if not validate_url(url):
        return None
    try:
        resp = requests.get(  # noqa: S113 - 超时显式设置
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"},
            allow_redirects=False,
        )
        text = resp.text[:200_000]
    except Exception:  # noqa: BLE001
        return None
    for pattern in _NICKNAME_PATTERNS:
        m = pattern.search(text)
        if m:
            name = html_lib.unescape(m.group(1)).strip()
            if name:
                return name[:64]
    return None


def _account_row(db, account_id: str) -> WxMpAccount | None:
    return db.scalar(select(WxMpAccount).where(WxMpAccount.account_id == account_id))


def _ensure_account(db, account_id: str, mp_name: str) -> WxMpAccount:
    row = _account_row(db, account_id)
    if row is None:
        row = WxMpAccount(account_id=account_id, mp_name=mp_name)
        db.add(row)
        db.flush()
    elif mp_name and not row.mp_name:
        row.mp_name = mp_name
    return row


def _user_sub_count(db, user_id: int) -> int:
    return int(db.scalar(
        select(func.count()).select_from(WxSubscription).where(
            WxSubscription.user_id == user_id,
            WxSubscription.active.is_(True),
        )
    ) or 0)


def _account_sub_count(db, account_id: str) -> int:
    return int(db.scalar(
        select(func.count()).select_from(WxSubscription).where(
            WxSubscription.account_id == account_id,
            WxSubscription.active.is_(True),
        )
    ) or 0)


def _active_account_count(db) -> int:
    return int(db.scalar(
        select(func.count()).select_from(WxMpAccount).where(WxMpAccount.active.is_(True))
    ) or 0)


def _display_name(account: WxMpAccount | None, account_id: str) -> str:
    return (account.mp_name if account and account.mp_name else "") or account_id


# ---------------------------------------------------------------------------
# /subscribe
# ---------------------------------------------------------------------------

@router.message(Command("subscribe"))
async def on_subscribe(message: types.Message) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, message.from_user.id)
        lang = user_language(user, message.from_user.language_code)
        if user is None or user.status == UserStatus.PENDING:
            await message.answer(t(lang, "user.pending", application_id="-"))
            return
        if user.status == UserStatus.DISABLED:
            await message.answer(t(lang, "user.disabled"))
            return

        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await _list_subscriptions(message, db, lang)
            return
        target = parts[1].strip()

        # 文章 URL → best-effort 提取号名；失败请用户直接发名称
        m = _ARTICLE_URL_RE.search(target)
        if m and "mp.weixin.qq.com" in m.group(0):
            name = _nickname_from_article(m.group(0))
            if not name:
                await message.answer(t(lang, "subscribe.name_needed"))
                return
        else:
            name = target[:64]
        if not name:
            await message.answer(t(lang, "subscribe.usage"))
            return

        await message.answer(t(lang, "subscribe.searching"))
        try:
            res = await weread_client.call_async("search", name)
        except (WereadTokenExpired, WereadVerifyNeeded) as e:
            logger.info("weread not logged in on search: %s", e)
            await message.answer(t(lang, "subscribe.need_login"))
            return
        except WereadError as e:
            await message.answer(t(lang, "subscribe.search_failed", err=str(e)[:200]))
            return

        results = (res.get("results") or [])[:5]
        if not results:
            await message.answer(t(lang, "subscribe.no_result"))
            return
        buttons = [
            [types.InlineKeyboardButton(
                text=f"{r.get('title') or name}（{r['account_id']}）",
                callback_data=f"wsubpick:{r['account_id']}",
            )]
            for r in results
        ]
        await message.answer(
            t(lang, "subscribe.pick_hint", name=name),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    finally:
        db.close()


async def _list_subscriptions(message: types.Message, db, lang: str) -> None:
    rows = db.execute(
        select(WxSubscription, WxMpAccount)
        .join(WxMpAccount, WxSubscription.account_id == WxMpAccount.account_id)
        .where(WxSubscription.user_id == message.from_user.id, WxSubscription.active.is_(True))
        .order_by(WxSubscription.created_at)
    ).all()
    if not rows:
        await message.answer(t(lang, "subscribe.list_empty"))
        return
    settings = get_settings()
    lines = [t(lang, "subscribe.list_header", count=len(rows), max=settings.wasub_max_subs_per_user)]
    buttons = []
    for i, (sub, account) in enumerate(rows, 1):
        is_auto = sub.delivery_mode == DeliveryMode.AUTO.value
        mode_label = t(lang, "subscribe.mode_auto" if is_auto else "subscribe.mode_notify")
        lines.append(t(lang, "subscribe.list_item", index=i,
                       name=_display_name(account, sub.account_id), mode=mode_label))
        buttons.append([
            types.InlineKeyboardButton(
                text=t(lang, "subscribe.btn_mode"),
                callback_data=f"wsubchg:{sub.id}:{'notify' if is_auto else 'auto'}",
            ),
            types.InlineKeyboardButton(
                text=t(lang, "subscribe.btn_unsub"),
                callback_data=f"wsubrm:{sub.id}",
            ),
        ])
    await message.answer(
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ---------------------------------------------------------------------------
# 订阅落库（pick/mode/fmt 共用）
# ---------------------------------------------------------------------------

async def _finish_subscribe(
    callback: types.CallbackQuery,
    account_id: str,
    mode: DeliveryMode,
    output_types: list[str],
    fmt_label: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(user, callback.from_user.language_code)

        settings = get_settings()
        existing = db.scalar(
            select(WxSubscription).where(
                WxSubscription.user_id == user.id,
                WxSubscription.account_id == account_id,
            )
        )
        if existing is None or not existing.active:
            if _user_sub_count(db, user.id) >= settings.wasub_max_subs_per_user:
                await callback.answer(
                    t(lang, "subscribe.cap_reached", max=settings.wasub_max_subs_per_user),
                    show_alert=True,
                )
                return

        # 服务端微信读书订阅（已 tracked 的号直接复用；未登录则明确提示）
        account = _account_row(db, account_id)
        if account is None or not account.active:
            try:
                await weread_client.call_async("subscribe", account_id)
            except (WereadTokenExpired, WereadVerifyNeeded) as e:
                logger.info("server subscribe blocked: %s", e)
                await callback.answer(t(lang, "subscribe.need_login"), show_alert=True)
                return
            except WereadError as e:
                await callback.answer(
                    t(lang, "subscribe.subscribe_failed", err=str(e)[:160]), show_alert=True
                )
                return
            account = _ensure_account(db, account_id, "")

        if account.active is False:
            account.active = True
        if _active_account_count(db) > settings.wasub_max_accounts:
            notify_admins(
                "accounts_full",
                f"⚠️ 全站公众号订阅已达软上限（{settings.wasub_max_accounts}），"
                "请评估 weread 账号风控压力。",
            )

        # 分发基线＝当前时刻：不分发历史文章（防首拉洪水）
        if existing is None:
            existing = WxSubscription(
                user_id=user.id,
                chat_id=callback.message.chat.id,
                account_id=account_id,
                delivery_mode=mode.value,
                output_types=list(output_types),
                last_article_time=int(time.time()),
            )
            db.add(existing)
            audit(db, action=AuditAction.SUBSCRIBE, operator_user_id=user.id,
                  target_type="wx_subscription", target_id=account_id,
                  details={"mode": mode.value})
            db.commit()
            if mode == DeliveryMode.AUTO:
                await callback.message.edit_text(
                    t(lang, "subscribe.created_auto",
                      name=_display_name(account, account_id), fmt=fmt_label or "PDF")
                )
            else:
                await callback.message.edit_text(
                    t(lang, "subscribe.created_notify",
                      name=_display_name(account, account_id))
                )
        else:
            existing.active = True
            existing.delivery_mode = mode.value
            existing.output_types = list(output_types)
            audit(db, action=AuditAction.SUBSCRIBE, operator_user_id=user.id,
                  target_type="wx_subscription", target_id=existing.id,
                  details={"mode": mode.value, "updated": True})
            db.commit()
            await callback.message.edit_text(
                t(lang, "subscribe.mode_changed",
                  mode=t(lang, "subscribe.mode_auto" if mode == DeliveryMode.AUTO
                         else "subscribe.mode_notify"))
            )
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("wsubpick:"))
async def on_pick(callback: types.CallbackQuery) -> None:
    account_id = callback.data.split(":", 1)[1]
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        account = _account_row(db, account_id)
        buttons = [
            [types.InlineKeyboardButton(
                text=t(lang, "subscribe.mode_notify_btn"),
                callback_data=f"wsubmode:{account_id}:notify",
            )],
            [types.InlineKeyboardButton(
                text=t(lang, "subscribe.mode_auto_btn"),
                callback_data=f"wsubmode:{account_id}:auto",
            )],
        ]
        await callback.message.edit_text(
            t(lang, "subscribe.mode_header", name=_display_name(account, account_id)),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("wsubmode:"))
async def on_mode(callback: types.CallbackQuery) -> None:
    _, account_id, mode = callback.data.split(":", 2)
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        if mode == DeliveryMode.NOTIFY.value:
            await _finish_subscribe(callback, account_id, DeliveryMode.NOTIFY, [])
            return
        buttons = [
            [types.InlineKeyboardButton(
                text=t(lang, f"format.{key}"),
                callback_data=f"wsubfmt:{account_id}:{key}",
            )]
            for key in ("pdf", "md", "img", "all")
        ]
        await callback.message.edit_text(
            t(lang, "subscribe.fmt_header", name=account_id),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("wsubfmt:"))
async def on_fmt(callback: types.CallbackQuery) -> None:
    _, key, fmt = callback.data.split(":", 2)
    output_types = _output_types(fmt)
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        if not output_types:
            await callback.answer(t(lang, "url.no_url_found"), show_alert=True)
            return
        if key.startswith("MP_WXS_"):
            await _finish_subscribe(
                callback, key, DeliveryMode.AUTO,
                [o.value for o in output_types], fmt_label=t(lang, f"format.{fmt}"),
            )
            return
        # 已有订阅切到 auto：wsubfmt:{subId}:{fmt}
        try:
            sub_id = int(key)
        except ValueError:
            await callback.answer()
            return
        sub = db.get(WxSubscription, sub_id)
        if sub is None or sub.user_id != user.id:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        sub.delivery_mode = DeliveryMode.AUTO.value
        sub.output_types = [o.value for o in output_types]
        audit(db, action=AuditAction.SUBSCRIBE, operator_user_id=user.id,
              target_type="wx_subscription", target_id=sub.id,
              details={"mode": "auto", "fmt": fmt})
        db.commit()
        await callback.message.edit_text(t(lang, "subscribe.mode_changed",
                                           mode=t(lang, "subscribe.mode_auto")))
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("wsubchg:"))
async def on_change_mode(callback: types.CallbackQuery) -> None:
    _, key, mode = callback.data.split(":", 2)
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        try:
            sub_id = int(key)
        except ValueError:
            await callback.answer()
            return
        sub = db.get(WxSubscription, sub_id)
        if sub is None or sub.user_id != user.id:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        if mode == DeliveryMode.NOTIFY.value:
            sub.delivery_mode = DeliveryMode.NOTIFY.value
            audit(db, action=AuditAction.SUBSCRIBE, operator_user_id=user.id,
                  target_type="wx_subscription", target_id=sub.id,
                  details={"mode": "notify", "updated": True})
            db.commit()
            await callback.message.edit_text(
                t(lang, "subscribe.mode_changed", mode=t(lang, "subscribe.mode_notify"))
            )
            await callback.answer()
            return
        # 切 auto：先选格式（wsubfmt:{subId}:{fmt}）
        buttons = [
            [types.InlineKeyboardButton(
                text=t(lang, f"format.{fmt_key}"),
                callback_data=f"wsubfmt:{sub.id}:{fmt_key}",
            )]
            for fmt_key in ("pdf", "md", "img", "all")
        ]
        await callback.message.edit_text(
            t(lang, "subscribe.fmt_header", name=sub.account_id),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("wsubrm:"))
async def on_unsubscribe(callback: types.CallbackQuery) -> None:
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        sub = db.get(WxSubscription, sub_id)
        if sub is None or sub.user_id != user.id:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        sub.active = False
        audit(db, action=AuditAction.UNSUBSCRIBE, operator_user_id=user.id,
              target_type="wx_subscription", target_id=sub.id)
        db.commit()

        name = sub.account_id
        account = _account_row(db, sub.account_id)
        if account is not None and account.mp_name:
            name = account.mp_name
        # 最后一个订阅者退订 → 服务端同步退订（best-effort），账号行停用
        if _account_sub_count(db, sub.account_id) == 0:
            try:
                await weread_client.call_async("unsubscribe", sub.account_id)
                if account is not None:
                    account.active = False
                    db.commit()
            except WereadError as e:
                logger.warning("server unsubscribe failed for %s: %s", sub.account_id, e)
        await callback.message.edit_text(t(lang, "subscribe.unsubscribed", name=name))
        await callback.answer()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 通知按钮 → 复用现有格式选择 FSM 建任务
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("wsubgo:"))
async def on_archive_from_notification(
    callback: types.CallbackQuery, state: FSMContext
) -> None:
    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None or user.status in (UserStatus.PENDING, UserStatus.DISABLED):
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        pending = db.get(WxPendingArticle, pending_id)
        if pending is None:
            await callback.answer(t(lang, "subscribe.pending_gone"), show_alert=True)
            return
        created_at = pending.created_at
        if created_at is not None:
            # SQLite 回读丢时区（DateTime 列不保留 tzinfo），按 UTC 处理
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > created_at + _PENDING_TTL:
                await callback.answer(t(lang, "subscribe.pending_gone"), show_alert=True)
                return
        # 所有权：仅该账号的 active 订阅者可点（callback 不携带敏感数据，但也不可滥用）
        subscribed = db.scalar(
            select(WxSubscription.id).where(
                WxSubscription.user_id == user.id,
                WxSubscription.account_id == pending.account_id,
                WxSubscription.active.is_(True),
            ).limit(1)
        )
        if subscribed is None:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return

        url = pending.doc_url
        if not url or not validate_url(url):
            await callback.answer(t(lang, "url.invalid"), show_alert=True)
            return
        await callback.message.answer(
            f"{t(lang, 'url.parsing')}\n"
            f"{t(lang, 'url.platform', platform=t(lang, 'platform.wechat'))}\n"
            f"{t(lang, 'url.title', title=url)}",
            reply_markup=format_selector(lang),
        )
        await state.update_data(
            pending_url=url, pending_platform=Platform.WECHAT.value
        )
        await state.set_state(ArchiveState.awaiting_format)
        await callback.answer()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 管理员：扫码登录 / 状态
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 管理员：扫码登录 / 状态（命令与管理中心按钮共用）
# ---------------------------------------------------------------------------

_LOGIN_IN_PROGRESS = False


def _admin_role_ok(db, user: object | None) -> bool:
    """管理中心按钮的 RBAC：数据库 role 校验（与 adm:* 回调一致）。"""
    from app.bot.keyboards import is_admin_role

    return user is not None and is_admin_role(user.role)


async def run_weread_login(message: types.Message, lang: str, operator_telegram_id: int) -> None:
    """扫码登录全流程：二维码 → 轮询 → 结果。成功记 WEREAD_LOGIN 审计。"""
    global _LOGIN_IN_PROGRESS
    if _LOGIN_IN_PROGRESS:
        await message.answer(t(lang, "weread.login_in_progress"))
        return
    _LOGIN_IN_PROGRESS = True
    try:
        msg = await message.answer(t(lang, "weread.login_started"))
        logged_in = False
        try:
            async for event in weread_client.login_events_async():
                if event.get("event") == "qr":
                    qr = _ascii_qr(event.get("url", ""))
                    await msg.edit_text(
                        f"<pre>{qr}</pre>\n{t(lang, 'weread.login_scan')}",
                        parse_mode="HTML",
                    )
                elif event.get("event") == "done":
                    logged_in = True
                    db = SessionLocal()
                    try:
                        audit(db, action=AuditAction.WEREAD_LOGIN,
                              operator_user_id=operator_telegram_id,
                              target_type="weread", target_id=str(event.get("account", "default")))
                        db.commit()
                    finally:
                        db.close()
                    await msg.edit_text(
                        t(lang, "weread.login_ok", account=event.get("account", "default"))
                    )
        except WereadError as e:
            logger.warning("weread login failed: %s", e)
            await msg.edit_text(t(lang, "weread.login_failed", err=str(e)[:200]))
            return
        if not logged_in:
            await msg.edit_text(t(lang, "weread.login_expired"))
    finally:
        _LOGIN_IN_PROGRESS = False


async def weread_status_text(db, lang: str) -> str:
    """微信读书 + 订阅状态页文案（/weread_status 命令与 adm:weread 页共用）。"""
    lines = [t(lang, "weread.status_header")]
    try:
        st = await weread_client.call_async("status")
        lines.append(t(lang, "weread.status_ok", account=st.get("account", "default")))
    except WereadError as e:
        lines.append(t(lang, "weread.status_bad", err=str(e)[:160]))
    accounts = db.scalars(
        select(WxMpAccount).where(WxMpAccount.active.is_(True)).order_by(WxMpAccount.created_at)
    ).all()
    if not accounts:
        lines.append(t(lang, "weread.no_accounts"))
    else:
        for account in accounts:
            lines.append(
                t(
                    lang,
                    "subscribe.list_status_line",
                    name=_display_name(account, account.account_id),
                    account_id=account.account_id,
                    count=_account_sub_count(db, account.account_id),
                    synckey=account.last_synckey or 0,
                )
            )
    return "\n".join(lines)


@router.message(Command("weread_login"))
async def weread_login_cmd(message: types.Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ 仅管理员可用")
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, message.from_user.id)
        lang = user_language(user, message.from_user.language_code)
    finally:
        db.close()
    await run_weread_login(message, lang, message.from_user.id)


@router.callback_query(F.data == "wlogin")
async def on_weread_login_button(callback: types.CallbackQuery) -> None:
    """管理中心「扫码登录」按钮（RBAC 与 adm:* 一致，走 DB role）。"""
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if not _admin_role_ok(db, user):
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
    finally:
        db.close()
    await callback.answer()
    await run_weread_login(callback.message, lang, callback.from_user.id)


@router.message(Command("weread_status"))
async def weread_status_cmd(message: types.Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ 仅管理员可用")
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, message.from_user.id)
        lang = user_language(user, message.from_user.language_code)
        await message.answer(await weread_status_text(db, lang))
    finally:
        db.close()
