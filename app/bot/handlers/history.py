"""历史记录（设计规格 §15/§16/§17/§18）。

获取文件只走 telegram_file_id（不访问原站）；重新抓取创建新任务。
所有 callback 均做服务端所有权校验（规格 §29/§30）。
"""

import logging

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.common import user_language
from app.bot.i18n import t
from app.bot.keyboards import main_menu
from app.database.database import SessionLocal
from app.database.enums import FileType, TaskStatus
from app.database.models import File, Task
from app.database.services import audit, get_user_by_telegram_id
from app.tasks import manager as task_manager
from app.tasks.queue import enqueue_task

logger = logging.getLogger(__name__)

router = Router(name="history")

PAGE_SIZE = 5
_FORMAT_LABEL = {
    FileType.PDF: "PDF",
    FileType.MARKDOWN: "MD",
    FileType.IMAGES_ZIP: "Images",
}


@router.callback_query(F.data == "menu:history")
async def history_list(callback: types.CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        await _render_history(callback, db, user.id, lang, page=1)
    finally:
        db.close()


@router.callback_query(F.data.startswith("hpage:"))
async def history_page(callback: types.CallbackQuery) -> None:
    try:
        page = max(1, int(callback.data.split(":", 1)[1]))
    except ValueError:
        page = 1
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        await _render_history(callback, db, user.id, lang, page)
    finally:
        db.close()


@router.callback_query(F.data.startswith("hist:"))
async def history_detail(callback: types.CallbackQuery) -> None:
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        try:
            task = task_manager.get_task_for_user(db, task_id, user.id)
        except task_manager.AccessDeniedError:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        await _render_detail(callback, db, task, lang)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 详情操作：获取文件 / 重新抓取 / 打开原文 / 删除
# ---------------------------------------------------------------------------

_FILE_TYPE_BY_CB = {
    "pdf": FileType.PDF,
    "md": FileType.MARKDOWN,
    "img": FileType.IMAGES_ZIP,
}


@router.callback_query(F.data.startswith("hf:"))
async def get_file(callback: types.CallbackQuery) -> None:
    """获取历史文件：只使用 telegram_file_id，不访问原网站（规格 §17）。"""
    _, task_id_s, type_s = callback.data.split(":", 2)
    file_type = _FILE_TYPE_BY_CB.get(type_s)
    if file_type is None:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        try:
            task = task_manager.get_task_for_user(db, int(task_id_s), user.id)
        except (task_manager.AccessDeniedError, ValueError):
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        file_ = next((f for f in task.files if f.type == file_type.value and f.telegram_file_id), None)
        if file_ is None:
            await callback.answer("no file", show_alert=True)
            return
        await callback.answer()
        await callback.bot.send_document(task.chat_id, file_.telegram_file_id)
        audit(db, action="FILE_SENT", operator_user_id=user.id,
              target_type="file", target_id=file_.id)
        db.commit()
    finally:
        db.close()


@router.callback_query(F.data.startswith("hra:"))
async def rearchive(callback: types.CallbackQuery) -> None:
    """重新抓取：创建新任务（旧记录保留，规格 §17/§57）。"""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        try:
            task = task_manager.get_task_for_user(db, task_id, user.id)
        except task_manager.AccessDeniedError:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return

        new_task = task_manager.create_task(
            db,
            user_id=user.id,
            chat_id=callback.message.chat.id,
            url=task.url,
            platform=task.platform or "web",
            output_types=task.output_types or ["PDF"],
        )
        db.commit()
        status_msg = await callback.message.answer(
            t(lang, "task.processing", task_id=new_task.id, platform=new_task.platform,
              status=t(lang, "status.queued"))
        )
        new_task.status_message_id = status_msg.message_id
        db.commit()
        audit(db, action="TASK_RETRY", operator_user_id=user.id,
              target_type="task", target_id=new_task.id, details={"from_task": task.id})
        db.commit()
        enqueue_task(new_task.id)
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("hurl:"))
async def open_original(callback: types.CallbackQuery) -> None:
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        try:
            task = task_manager.get_task_for_user(db, task_id, user.id)
        except task_manager.AccessDeniedError:
            await callback.answer()
            return
        await callback.answer()
        await callback.message.answer(task.url)
    finally:
        db.close()


@router.callback_query(F.data.startswith("hdel:"))
async def delete_record(callback: types.CallbackQuery) -> None:
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer()
            return
        lang = user_language(user, callback.from_user.language_code)
        try:
            task = task_manager.get_task_for_user(db, task_id, user.id)
        except task_manager.AccessDeniedError:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        for f in list(task.files):
            db.delete(f)
        db.delete(task)
        audit(db, action="FILE_DELETED", operator_user_id=user.id,
              target_type="task", target_id=task_id)
        db.commit()
        await callback.answer()
        await callback.message.edit_text(t(lang, "history.empty"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

def _outputs_label(task: Task) -> str:
    parts = []
    for f in task.files:
        try:
            ft = FileType(f.type)
        except ValueError:
            continue
        if ft in _FORMAT_LABEL and _FORMAT_LABEL[ft] not in parts:
            parts.append(_FORMAT_LABEL[ft])
    return "+".join(parts) or "-"


def _task_row_label(task: Task) -> str:
    title = task.title or task.url[:40]
    date = (task.created_at.strftime("%Y-%m-%d") if task.created_at else "-")
    return f"{title}\n{date} · {_outputs_label(task)}"


async def _render_history(callback: types.CallbackQuery, db, user_id: int, lang: str, page: int) -> None:
    tasks, total = task_manager.list_user_tasks(db, user_id, page=page, per_page=PAGE_SIZE)
    if not tasks:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu")]])
        await callback.message.edit_text(t(lang, "history.empty"), reply_markup=kb)
        await callback.answer()
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [t(lang, "history.title", total=total)]
    for i, task in enumerate(tasks, start=1):
        lines.append(f"{i}. {_task_row_label(task)}")

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text=t(lang, "action.prev"), callback_data=f"hpage:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page} / {total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text=t(lang, "action.next"), callback_data=f"hpage:{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=task.title or task.url[:20], callback_data=f"hist:{task.id}") for task in tasks],
        nav,
        [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu")],
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()


async def _render_detail(callback: types.CallbackQuery, db, task: Task, lang: str) -> None:
    size_mb = sum(f.size for f in task.files) / (1024 * 1024)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t(lang, "action.get_pdf"), callback_data=f"hf:{task.id}:pdf"),
            InlineKeyboardButton(text=t(lang, "action.get_markdown"), callback_data=f"hf:{task.id}:md"),
        ],
        [
            InlineKeyboardButton(text=t(lang, "action.get_images"), callback_data=f"hf:{task.id}:img"),
        ],
        [
            InlineKeyboardButton(text=t(lang, "action.rearchive"), callback_data=f"hra:{task.id}"),
            InlineKeyboardButton(text=t(lang, "action.open_original"), callback_data=f"hurl:{task.id}"),
        ],
        [InlineKeyboardButton(text=t(lang, "action.delete"), callback_data=f"hdel:{task.id}")],
        [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu:history")],
    ])
    archived_at = task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "-"
    text = t(
        lang,
        "history.detail",
        title=task.title or task.url,
        source=task.platform or "-",
        author=task.author or "-",
        archived_at=archived_at,
        url=task.url,
        formats=_outputs_label(task),
        size=f"{size_mb:.1f} MB",
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()
