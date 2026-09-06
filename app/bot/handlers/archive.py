"""URL 下载流程（设计规格 §6/§7/§8/§30/§54）。

消息 URL → 校验(SSRF/格式) → 识别平台 → 预览 → 格式选择(FSM) → 建任务入队
→ 状态消息[取消] → worker 完成后交付。
"""

import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.archive.detector import detect, extract_first_url, platform_label_key
from app.archive.ssrf import validate_url
from app.bot.common import user_language
from app.bot.i18n import t
from app.bot.keyboards import cancel_button, format_selector
from app.config import get_settings
from app.database.database import SessionLocal
from app.database.enums import OutputType, Platform, TaskStatus, UserStatus
from app.database.services import get_user_by_telegram_id
from app.tasks import manager as task_manager
from app.tasks.manager import TaskLimitError
from app.tasks.queue import enqueue_task

logger = logging.getLogger(__name__)

router = Router(name="archive")


class ArchiveState(StatesGroup):
    awaiting_format = State()  # 等待选择输出格式


_URL_RE = F.text.regexp(r"https?://")


@router.message(_URL_RE)
async def on_url_message(message: types.Message, state: FSMContext) -> None:
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

        url = extract_first_url(message.text or "")
        if not url or not validate_url(url):
            await message.answer(t(lang, "url.invalid"))
            return

        platform = detect(url)
        # 合并「解析中」与「预览+格式选择」为一条消息：省一次 Telegram API
        # 往返（VPS 上每次 RTT ≈ 0.5s，用户感知的“bot 反应慢”主要来自这里）
        await message.answer(
            f"{t(lang, 'url.parsing')}\n"
            f"{t(lang, 'url.platform', platform=t(lang, platform_label_key(platform)))}\n"
            f"{t(lang, 'url.title', title=url)}",
            reply_markup=format_selector(lang),
        )
        await state.update_data(pending_url=url, pending_platform=platform.value)
        await state.set_state(ArchiveState.awaiting_format)
    finally:
        db.close()


@router.callback_query(ArchiveState.awaiting_format, F.data.startswith("fmt:"))
async def on_format_selected(callback: types.CallbackQuery, state: FSMContext) -> None:
    # 选择后立即收起格式菜单：作为点击反馈，也防止重复点击/误触
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - 消息可能过旧/已被编辑，尽力而为
        pass

    data = await state.get_data()
    url: str | None = data.get("pending_url")
    platform: str | None = data.get("pending_platform")
    fmt = callback.data.split(":", 1)[1]

    output_types = _output_types(fmt)
    await state.clear()

    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        if user is None:
            await callback.answer("denied", show_alert=True)
            return
        lang = user_language(user, callback.from_user.language_code)
        if not url or not platform:
            await callback.answer()
            await callback.message.answer(t(lang, "url.no_url_found"))
            return

        try:
            # 特殊网站（财新等 WEB）按 url 自动关联可用 cookie profile
            auto_profile = None
            try:
                from app.archive.cookie_registry import SPECIAL_SITES as _SS
                from app.config import get_settings as _GS
                # 文件优先：profile 文件会被运行时回写/更新，settings 有 lru_cache 旧值
                _profs = {}
                _pf = _GS().cookie_profiles_file
                if _pf:
                    from pathlib import Path as _Path
                    import json as _json
                    _pp = _Path(_pf)
                    if not _pp.is_absolute():
                        _pp = _Path("/app") / _pp
                    if _pp.exists():
                        try:
                            _loaded = _json.loads(_pp.read_text(encoding="utf-8"))
                            if isinstance(_loaded, dict):
                                _profs = _loaded
                        except Exception:
                            _profs = {}
                if not _profs:
                    _profs = _GS().cookie_profiles or {}
                if platform == Platform.WEB.value:
                    for _k, _cfg in _SS.items():
                        if _cfg.get("platform") != Platform.WEB.value:
                            continue
                        for _dom in _cfg.get("domains", []):
                            if _dom.lstrip(".") in (url or ""):
                                if _k in _profs:
                                    auto_profile = _k
                                    break
                        if auto_profile:
                            break
                elif platform.value in ("twitter", "zhihu", "xhs", "reddit", "wechat"):
                    # 登录类平台：若某 profile 配置了该平台的 cookie，自动关联
                    # （twitter→"x"、zhihu→"zhihu"、wechat→"wechat"、caixin 走上面 WEB 分支）
                    for _k, _platforms in _profs.items():
                        if _platforms.get(platform.value):
                            auto_profile = _k
                            break
            except Exception:
                pass
            task = task_manager.create_task(
                db,
                user_id=user.id,
                chat_id=callback.message.chat.id,
                url=url,
                platform=platform,
                output_types=[o.value for o in output_types],
                cookie_profile=auto_profile,
            )
            db.commit()
        except TaskLimitError as e:
            key = "error.storage_full" if e.code == "STORAGE_FULL" else "error.unknown"
            await callback.message.answer(t(lang, key))
            return

        status_msg = await callback.message.answer(
            t(lang, "task.processing", task_id=task.id, platform=platform, status=t(lang, "status.queued")),
            reply_markup=cancel_button(lang, task.id),
        )
        task.status_message_id = status_msg.message_id
        db.add(task)
        db.commit()

        enqueue_task(task.id)
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data.startswith("fmt:"))
async def on_format_stale(callback: types.CallbackQuery) -> None:
    """状态已失效（会话清空/重启后）的格式按钮兜底：收起菜单，消除加载动画。

    注意注册顺序：必须在 on_format_selected 之后，仅接管状态已不匹配的回调。
    """
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: types.CallbackQuery) -> None:
    """取消任务（规格 §54）：服务端解析/查库/所有权校验后执行。"""
    try:
        task_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return

    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, callback.from_user.id)
        lang = user_language(user, callback.from_user.language_code)
        if user is None:
            await callback.answer()
            return
        task = task_manager.get_task(db, task_id)
        if task is None or task.user_id != user.id:
            await callback.answer(t(lang, "user.denied"), show_alert=True)
            return
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            await callback.answer()
            return
        task_manager.request_cancel(db, task)
        await callback.answer()
        await callback.message.edit_text(t(lang, "action.cancel"))
    finally:
        db.close()


def _output_types(fmt: str) -> list[OutputType]:
    settings = get_settings()
    if fmt == "pdf":
        return [OutputType.PDF] if settings.pdf_enabled else []
    if fmt == "md":
        return [OutputType.MARKDOWN] if settings.markdown_enabled else []
    if fmt == "img":
        return [OutputType.IMAGES] if settings.image_enabled else []
    if fmt == "all":
        types_ = []
        if settings.pdf_enabled:
            types_.append(OutputType.PDF)
        if settings.markdown_enabled:
            types_.append(OutputType.MARKDOWN)
        if settings.image_enabled:
            types_.append(OutputType.IMAGES)
        return types_
    return []
