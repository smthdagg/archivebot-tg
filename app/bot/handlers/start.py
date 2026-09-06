"""/start 与注册/审批状态处理（设计规格 §4/§21/§22）。

新用户准入：配置了 REGISTRATION_CODE 时，/start 后需输入申请暗号，
暗号正确才创建申请单进入管理员审批队列；未配置则保持开放申请。
同一用户的未决（PENDING）申请复用原申请 ID，不重复建单。
"""

import logging

from aiogram import F, Router, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.bot.common import ensure_user, user_language
from app.bot.i18n import t
from app.bot.keyboards import main_menu
from app.database.database import SessionLocal
from app.database.enums import UserStatus
from app.database.models import UserApplication
from app.database.services import (
    add_registration_blocklist,
    audit,
    get_registration_blocklist,
    get_registration_code,
)

logger = logging.getLogger(__name__)

router = Router(name="start")


class RegistrationState(StatesGroup):
    awaiting_code = State()  # 等待用户输入申请暗号


@router.message(CommandStart())
async def on_start(message: types.Message, state: FSMContext) -> None:
    db = SessionLocal()
    try:
        user, is_new = ensure_user(
            db,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            display_name=message.from_user.full_name,
            telegram_lang=message.from_user.language_code,
        )
        lang = user_language(user, message.from_user.language_code)

        if is_new:
            audit(db, action="USER_REGISTERED", operator_user_id=user.id, target_type="user", target_id=user.id)

        # 暗号爆破黑名单：直接拒绝，不进入申请流程
        if user.status != UserStatus.ACTIVE and message.from_user.id in get_registration_blocklist(db):
            await message.answer(t(lang, "user.blocked"))
            return

        if user.status == UserStatus.PENDING:
            await _handle_pending(message, state, db, user, lang)
            return

        if user.status == UserStatus.DISABLED:
            await message.answer(t(lang, "user.disabled"))
            return

        await state.clear()
        # ACTIVE / DELETED（DELETED 视为重新注册）
        await message.answer(
            t(lang, "start.welcome"),
            reply_markup=main_menu(lang, is_admin=_is_admin(user)),
        )
    finally:
        db.close()


async def _handle_pending(message: types.Message, state: FSMContext, db, user, lang: str) -> None:
    """PENDING 用户：站点开启暗号且尚无未决申请时，先索要暗号；通过才建单。"""
    code_required = bool(get_registration_code(db))

    if code_required and not _has_pending_application(db, user.telegram_id):
        # 进入暗号输入态（此时还不建申请单，避免垃圾申请占位）
        await state.set_state(RegistrationState.awaiting_code)
        await state.update_data(reg_user_id=user.id, reg_lang=lang)
        await message.answer(t(lang, "user.enter_code"))
        return

    application = _latest_or_new_application(db, user, message)
    audit(db, action="USER_APPLY", operator_user_id=user.id, target_type="user", target_id=user.id)
    db.commit()
    await state.clear()
    await message.answer(t(lang, "user.pending", application_id=application.id))


@router.message(F.text == "/cancel", RegistrationState.awaiting_code)
async def on_code_cancel(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer(t(data.get("reg_lang", "zh-CN"), "user.reg_cancelled"))


@router.message(RegistrationState.awaiting_code, F.text)
async def on_code_entered(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("reg_lang", "zh-CN")

    db = SessionLocal()
    expected = get_registration_code(db).strip()
    if (message.text or "").strip() != expected:
        failures = int(data.get("code_failures", 0)) + 1
        if failures >= 3:
            # 爆破防护：连续 3 次输错，踢出并拉黑（管理员可在 🔑 页面解除）
            add_registration_blocklist(db, message.from_user.id)
            db.commit()
            await state.clear()
            audit(db, action="USER_BLOCKED", operator_user_id=message.from_user.id,
                  target_type="user", target_id=message.from_user.id,
                  details={"reason": "registration code brute force"})
            await message.answer(t(lang, "user.too_many_attempts"))
            return
        await state.update_data(code_failures=failures)
        remaining = 3 - failures
        left = t(lang, "user.code_attempts_left", remaining=remaining)
        await message.answer(t(lang, "user.code_invalid") + "\n⚠️ " + left)
        return

    try:
        # 暗号正确 → 创建申请单（进入审批队列）
        class _RefUser:
            id = data["reg_user_id"]
            telegram_id = message.from_user.id
            username = message.from_user.username

        application = _latest_or_new_application(db, _RefUser(), message)
        audit(db, action="USER_APPLY", operator_user_id=data["reg_user_id"],
              target_type="user", target_id=data["reg_user_id"])
        db.commit()
        await state.clear()
        await message.answer(t(lang, "user.pending", application_id=application.id))
    finally:
        db.close()


@router.message(RegistrationState.awaiting_code)
async def on_code_non_text(message: types.Message, state: FSMContext) -> None:
    """暗号输入态收到非文本消息（贴纸等）：重申提示。"""
    data = await state.get_data()
    await message.answer(t(data.get("reg_lang", "zh-CN"), "user.enter_code"))


def _has_pending_application(db, telegram_id: int) -> bool:
    return db.query(UserApplication).filter(
        UserApplication.telegram_id == telegram_id,
        UserApplication.status == "PENDING",
    ).first() is not None


def _latest_or_new_application(db, user, message: types.Message) -> UserApplication:
    """同一用户的未决申请直接复用（防重复建单），否则新建。"""
    existing = db.query(UserApplication).filter(
        UserApplication.telegram_id == user.telegram_id,
        UserApplication.status == "PENDING",
    ).first()
    if existing is not None:
        return existing
    app = UserApplication(
        telegram_id=user.telegram_id,
        username=user.username,
        message=message.text or "",
        status="PENDING",
    )
    db.add(app)
    db.flush()
    return app


def _is_admin(user) -> bool:
    from app.database.enums import UserRole

    return user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN)


@router.callback_query(lambda c: c.data == "menu")
async def back_to_menu(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        from app.database.services import get_user_by_telegram_id

        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        await callback.message.edit_text(
            t(lang, "main.menu"),
            reply_markup=main_menu(lang, is_admin=_is_admin(user)),
        )
    finally:
        db.close()
    await callback.answer()
