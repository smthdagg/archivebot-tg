"""主菜单各项：新建/搜索/统计/设置/帮助（规格 §5/§18/§19/§20）。"""

import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.bot.common import user_language
from app.bot.i18n import t
from app.bot.keyboards import main_menu
from app.database.database import SessionLocal
from app.database.models import Task
from app.database.services import get_user_by_telegram_id

logger = logging.getLogger(__name__)

router = Router(name="menu")


class SearchState(StatesGroup):
    query = State()


def _back_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu")]]
    )


@router.callback_query(F.data == "menu:new")
async def new_download(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        await callback.message.edit_text(t(lang, "url.no_url_found"), reply_markup=_back_kb(lang))
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def help_menu(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        await callback.message.edit_text(t(lang, "help.text"), reply_markup=_back_kb(lang))
    finally:
        db.close()
    await callback.answer()


# ---------------------------------------------------------------------------
# 搜索（规格 §18）
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:search")
async def search_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        await callback.message.edit_text(t(lang, "search.prompt"), reply_markup=_back_kb(lang))
        await state.set_state(SearchState.query)
    finally:
        db.close()
    await callback.answer()


@router.message(SearchState.query)
async def search_execute(message: types.Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    await state.clear()
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, message.from_user.id)
        if user is None:
            return
        lang = user_language(user, message.from_user.language_code)
        pattern = f"%{query}%"
        stmt = (
            select(Task)
            .where(
                Task.user_id == user.id,
                (Task.title.like(pattern)) | (Task.url.like(pattern)) | (Task.platform.like(pattern)),
            )
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        tasks = list(db.scalars(stmt))
        if not tasks:
            await message.answer(t(lang, "search.empty"))
            return
        lines = [t(lang, "search.results")]
        for i, task in enumerate(tasks, start=1):
            title = task.title or task.url[:40]
            lines.append(f"{i}. {title}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=str(i), callback_data=f"hist:{task.id}") for i, task in enumerate(tasks, start=1)],
            [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu")],
        ])
        await message.answer("\n".join(lines), reply_markup=kb)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 统计（规格 §19）
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:stats")
async def stats(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        total = db.scalar(select(func.count(Task.id)).where(Task.user_id == user.id)) or 0
        completed = db.scalar(
            select(func.count(Task.id)).where(Task.user_id == user.id, Task.status == "COMPLETED")
        ) or 0
        text = (
            f"📊 {t(lang, 'menu.stats')}\n\n"
            f"总任务 / Total: {total}\n"
            f"已完成 / Completed: {completed}\n"
        )
        await callback.message.edit_text(text, reply_markup=_back_kb(lang))
    finally:
        db.close()
    await callback.answer()


# ---------------------------------------------------------------------------
# 设置（规格 §20）—— MVP 占位
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:settings")
async def settings(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        text = (
            f"{t(lang, 'settings.title')}\n\n"
            f"{t(lang, 'settings.language')} {lang}"
        )
        await callback.message.edit_text(text, reply_markup=_back_kb(lang))
    finally:
        db.close()
    await callback.answer()
