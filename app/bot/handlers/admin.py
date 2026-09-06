"""Bot 管理中心（设计规格 §24-§27）：待审核、用户、任务、系统状态、日志。

所有操作服务端做 RBAC 校验（规格 §30/§50）。
"""

import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.bot.common import user_language
from app.bot.i18n import t
from app.bot.keyboards import admin_menu, is_admin_role
from app.database.database import SessionLocal
from app.database.enums import ApplicationStatus, AuditAction, UserStatus
from app.database.models import AuditLog, User, UserApplication
from app.database.services import (
    audit,
    get_registration_code,
    get_user_by_telegram_id,
    now_utc,
    set_setting,
)
from app.storage.manager import get_storage
from app.tasks.queue import queue_stats

logger = logging.getLogger(__name__)

router = Router(name="admin")


class AdminState(StatesGroup):
    awaiting_regcode = State()


def _back_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu:admin")]]
    )


@router.callback_query(F.data == "menu:admin")
async def admin_center(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None or not is_admin_role(user.role):
            await callback.answer(t(user_language(user), "user.denied"), show_alert=True)
            return
        lang = user_language(user, callback.from_user.language_code)
        await callback.message.edit_text(t(lang, "admin.center"), reply_markup=admin_menu(lang))
    finally:
        db.close()
    await callback.answer()


# ---------------------------------------------------------------------------
# 申请暗号（管理员查看/修改；存 system_settings，Web Admin 同步可改）
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "adm:regcode")
async def regcode_view(callback: types.CallbackQuery, state: FSMContext) -> None:
    db = SessionLocal()
    try:
        admin = get_user_by_telegram_id(db, callback.from_user.id)
        if admin is None or not is_admin_role(admin.role):
            await callback.answer(t(user_language(admin), "user.denied"), show_alert=True)
            return
        lang = user_language(admin)
        code = get_registration_code(db) or "—（开放申请）"
        await state.clear()
        await callback.message.edit_text(
            t(lang, "admin.regcode.view", code=code),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "admin.regcode.edit").split("：")[0].split(":")[0][:16] or "✏️",
                                      callback_data="adm:regcode:edit")],
                [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu:admin")],
            ]),
        )
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data == "adm:regcode:edit")
async def regcode_edit_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    db = SessionLocal()
    try:
        admin = get_user_by_telegram_id(db, callback.from_user.id)
        if admin is None or not is_admin_role(admin.role):
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(admin)
        await state.set_state(AdminState.awaiting_regcode)
        await state.update_data(regcode_admin_id=admin.id, regcode_lang=lang)
        await callback.message.edit_text(t(lang, "admin.regcode.edit"))
    finally:
        db.close()
    await callback.answer()


@router.message(AdminState.awaiting_regcode, F.text == "/cancel")
async def regcode_edit_cancel(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer(t(data.get("regcode_lang", "zh-CN"), "action.cancel"))


@router.message(AdminState.awaiting_regcode, F.text)
async def regcode_edit_save(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("regcode_lang", "zh-CN")
    new_code = (message.text or "").strip()
    if len(new_code) > 64:
        await message.answer(t(lang, "error.unknown"))
        return
    db = SessionLocal()
    try:
        set_setting(db, "registration_code", new_code, operator_user_id=data.get("regcode_admin_id"))
        db.commit()
        await state.clear()
        await message.answer(t(lang, "admin.regcode.saved", code=new_code or "—"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 待审核
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:pending")
async def pending_list(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None or not is_admin_role(user.role):
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(user, callback.from_user.language_code)
        apps = list(
            db.scalars(
                select(UserApplication)
                .where(UserApplication.status == ApplicationStatus.PENDING)
                .order_by(UserApplication.created_at.desc())
                .limit(10)
            )
        )
        if not apps:
            await callback.message.edit_text("🛡 暂无待审核申请", reply_markup=_back_kb(lang))
            await callback.answer()
            return
        lines = [f"🛡 {t(lang, 'admin.pending')}  ({len(apps)})"]
        for i, app in enumerate(apps, start=1):
            name = app.username or str(app.telegram_id)
            created = app.created_at.strftime("%H:%M") if app.created_at else "-"
            lines.append(f"{i}️⃣ @{name}  申请时间：{created}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=str(i), callback_data=f"admappr:{app.id}")
                    for i, app in enumerate(apps, start=1)],
            [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu:admin")],
        ])
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("admappr:"))
async def application_detail(callback: types.CallbackQuery) -> None:
    try:
        app_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        admin = get_user_by_telegram_id(db, callback.from_user.id)
        if admin is None or not is_admin_role(admin.role):
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(admin, callback.from_user.language_code)
        app = db.get(UserApplication, app_id)
        if app is None:
            await callback.answer()
            return
        user = db.scalar(select(User).where(User.telegram_id == app.telegram_id))
        text = (
            f"👤 @{app.username or app.telegram_id}\n\n"
            f"Telegram ID: {app.telegram_id}\n"
            f"申请时间: {app.created_at.strftime('%Y-%m-%d %H:%M') if app.created_at else '-'}\n"
            f"历史任务: {len(user.tasks) if user else 0}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ 批准", callback_data=f"apprv:{app.id}:approve"),
                InlineKeyboardButton(text="❌ 拒绝", callback_data=f"apprv:{app.id}:reject"),
            ],
            [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="adm:pending")],
        ])
        await callback.message.edit_text(text, reply_markup=kb)
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("apprv:"))
async def review_application(callback: types.CallbackQuery) -> None:
    _, app_id_s, decision = callback.data.split(":", 2)
    db = SessionLocal()
    try:
        admin = get_user_by_telegram_id(db, callback.from_user.id)
        if admin is None or not is_admin_role(admin.role):
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(admin, callback.from_user.language_code)
        app = db.get(UserApplication, int(app_id_s))
        if app is None or app.status != ApplicationStatus.PENDING:
            await callback.answer()
            return
        user = db.scalar(select(User).where(User.telegram_id == app.telegram_id))
        if decision == "approve" and user is not None:
            user.status = UserStatus.ACTIVE
            user.approved_at = now_utc()
            audit(db, action=AuditAction.USER_APPROVE, operator_user_id=admin.id,
                  target_type="user", target_id=user.id)
        else:
            audit(db, action=AuditAction.USER_REJECTED, operator_user_id=admin.id,
                  target_type="user", target_id=user.id if user else app.telegram_id)
        app.status = ApplicationStatus.APPROVED if decision == "approve" else ApplicationStatus.REJECTED
        app.reviewed_by = admin.id
        app.reviewed_at = now_utc()
        db.commit()
        await callback.answer("✅" if decision == "approve" else "❌")
        await callback.message.edit_text(t(lang, "admin.pending"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 系统状态（规格 §38）
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:status")
async def system_status(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        admin = get_user_by_telegram_id(db, callback.from_user.id)
        if admin is None or not is_admin_role(admin.role):
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(admin, callback.from_user.language_code)

        storage = get_storage()
        total_mb = storage.total_size() / (1024 * 1024)
        hard_mb = storage.hard_limit / (1024 * 1024)
        qstats = queue_stats()
        users_active = db.scalar(select(func.count(User.id)).where(User.status == UserStatus.ACTIVE)) or 0
        users_pending = db.scalar(select(func.count(User.id)).where(User.status == UserStatus.PENDING)) or 0

        text = (
            f"📊 {t(lang, 'admin.status')}\n\n"
            f"Storage: {total_mb:.0f} MB / {hard_mb:.0f} MB\n"
            f"Queue: Waiting {qstats['waiting']} · Running {qstats['started']} · Failed {qstats['failed']}\n"
            f"Users: ACTIVE {users_active} · PENDING {users_pending}\n"
        )
        await callback.message.edit_text(text, reply_markup=_back_kb(lang))
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data == "adm:logs")
async def logs(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        admin = get_user_by_telegram_id(db, callback.from_user.id)
        if admin is None or not is_admin_role(admin.role):
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(admin, callback.from_user.language_code)
        logs_ = list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10)))
        if not logs_:
            await callback.message.edit_text("📜 暂无日志", reply_markup=_back_kb(lang))
        else:
            lines = ["📜 最近日志"]
            for log in logs_:
                created = log.created_at.strftime("%m-%d %H:%M") if log.created_at else "-"
                lines.append(f"{created} {log.action} #{log.target_id or '-'}")
            await callback.message.edit_text("\n".join(lines), reply_markup=_back_kb(lang))
    finally:
        db.close()
    await callback.answer()
