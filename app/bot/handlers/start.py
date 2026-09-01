"""/start 与注册/审批状态处理（设计规格 §4/§21/§22）。"""

import logging

from aiogram import Router, types
from aiogram.filters import CommandStart

from app.bot.common import ensure_user, user_language
from app.bot.i18n import t
from app.bot.keyboards import main_menu
from app.database.database import SessionLocal
from app.database.enums import UserStatus
from app.database.models import UserApplication
from app.database.services import audit

logger = logging.getLogger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def on_start(message: types.Message) -> None:
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

        if user.status == UserStatus.PENDING:
            application = _create_application(db, user, message)
            audit(db, action="USER_APPLY", operator_user_id=user.id, target_type="user", target_id=user.id)
            db.commit()
            await message.answer(
                t(lang, "user.pending", application_id=application.id)
            )
            return

        if user.status == UserStatus.DISABLED:
            await message.answer(t(lang, "user.disabled"))
            return

        # ACTIVE / DELETED（DELETED 视为重新注册）
        await message.answer(
            t(lang, "start.welcome"),
            reply_markup=main_menu(lang, is_admin=_is_admin(user)),
        )
    finally:
        db.close()


def _is_admin(user) -> bool:
    from app.database.enums import UserRole

    return user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN)


def _create_application(db, user, message: types.Message) -> UserApplication:
    app = UserApplication(
        telegram_id=user.telegram_id,
        username=user.username,
        message=message.text or "",
        status="PENDING",
    )
    db.add(app)
    db.flush()
    return app


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
