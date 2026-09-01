"""rq worker 的任务执行（设计规格 §35/§53）。

process_task 是 rq 入口（同步函数）；内部按状态机推进，并调用 delivery
向 Telegram 交付文件与完成消息。
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.archive.runner import run_archive
from app.bot import delivery
from app.bot.i18n import t
from app.config import get_settings
from app.database.database import SessionLocal
from app.database.enums import (
    AuditAction,
    ErrorCode,
    FileType,
    OutputType,
    Platform,
    TaskStatus,
)
from app.database.models import File, Task, User
from app.database.services import audit
from app.storage.cleanup import cleanup_if_needed
from app.storage.manager import get_storage
from app.tasks import manager as task_manager
from app.tasks.manager import TaskLimitError
from app.tasks.queue import enqueue_task_retry
from app.tasks.retry import is_retryable

logger = logging.getLogger(__name__)

# 处理中状态的 i18n key（设计规格 §8 状态展示）
_STATUS_I18N = {
    TaskStatus.QUEUED: "status.queued",
    TaskStatus.FETCHING: "status.fetching",
    TaskStatus.PARSING: "status.parsing",
    TaskStatus.DOWNLOADING_IMAGES: "status.downloading_images",
    TaskStatus.GENERATING_MARKDOWN: "status.generating_markdown",
    TaskStatus.GENERATING_PDF: "status.generating_pdf",
    TaskStatus.UPLOADING: "status.uploading",
}


def process_task(task_id: int) -> dict:
    """执行一个归档任务。rq 入口。"""
    db = SessionLocal()
    try:
        return _process(db, task_id)
    finally:
        db.close()


def _process(db, task_id: int) -> dict:
    settings = get_settings()
    task = task_manager.get_task(db, task_id)
    if task is None:
        logger.warning("task %s not found", task_id)
        return {"status": "NOT_FOUND"}
    if task.status != TaskStatus.QUEUED:
        logger.info("task %s already %s, skip", task_id, task.status)
        return {"status": task.status}

    user = db.get(User, task.user_id)
    lang = user.language if user else settings.default_language

    # 并发限制（规格 §34）：单用户/全局上限
    if (
        task_manager.user_active_task_count(db, task.user_id) >= settings.max_user_concurrency
        or task_manager.global_active_task_count(db) >= settings.max_global_concurrency
    ):
        _fail(db, task, ErrorCode.UNKNOWN, "Concurrency limit reached", lang)
        return {"status": TaskStatus.FAILED}

    storage = get_storage()
    task_dir = storage.task_dir(task.storage_uuid) if task.storage_uuid else None
    if task_dir is None or not task_dir.exists():
        _fail(db, task, ErrorCode.UNKNOWN, "Task storage missing", lang)
        return {"status": TaskStatus.FAILED}

    output_types = [OutputType(v) for v in (task.output_types or [])]

    def on_status(status: TaskStatus) -> None:
        task_manager.set_status(db, task, status)
        db.commit()
        _update_status_message(db, task, lang)

    def on_progress() -> None:
        # 取消检查点：worker 尽快终止（规格 §54）
        db.refresh(task)
        if task_manager.is_cancelled(task):
            raise _Cancelled()

    try:
        on_status(TaskStatus.FETCHING)
        on_progress()

        result = run_archive(
            task_dir=task_dir,
            url=task.url,
            platform=Platform(task.platform) if task.platform else Platform.WEB,
            output_types=output_types,
            on_status=lambda s: (on_status(s), on_progress()),
        )

        on_progress()

        # ---- 上传 Telegram（规格 §13/§37）----
        on_status(TaskStatus.UPLOADING)
        uploaded, skipped = asyncio.run(_upload_all(db, task, result))
        if not uploaded and (result.pdf_path or result.markdown_path or result.images):
            raise TaskLimitError(ErrorCode.TELEGRAM_UPLOAD_FAILED, "No artifact delivered")
        if skipped:
            logger.info("task %s skipped oversized artifacts: %s", task.id, skipped)

        # ---- 完成 ----
        task.title = result.title or task.title
        task.author = result.author or task.author
        task.published_at = result.published_at or task.published_at
        task.excerpt = result.excerpt
        task_manager.set_status(db, task, TaskStatus.COMPLETED)
        audit(db, action=AuditAction.TASK_COMPLETED, operator_user_id=task.user_id,
              target_type="task", target_id=task.id)
        db.commit()

        asyncio.run(delivery.send_message(task.chat_id, _completion_text(task, lang)))
        if skipped:
            names = "\n".join(f"• {name}" for name in skipped)
            asyncio.run(delivery.send_message(
                task.chat_id,
                t(lang, "archive.oversized_skipped", files=names),
            ))
        _cleanup_local(db, task)
        _ensure_soft_limit_cleanup(db, task.storage_uuid)
        return {"status": TaskStatus.COMPLETED}

    except _Cancelled:
        task_manager.set_status(db, task, TaskStatus.CANCELLED)
        audit(db, action=AuditAction.TASK_CANCELLED, operator_user_id=task.user_id,
              target_type="task", target_id=task.id)
        db.commit()
        asyncio.run(delivery.edit_message(task.chat_id, task.status_message_id or 0, t(lang, "action.cancel")))
        return {"status": TaskStatus.CANCELLED}
    except Exception as e:  # noqa: BLE001
        logger.exception("task %s failed", task_id)
        code = getattr(e, "code", None) or (
            # aiogram 抛出的 Telegram API 错误（如 401/网络问题）归为上传失败
            ErrorCode.TELEGRAM_UPLOAD_FAILED
            if type(e).__module__.startswith("aiogram")
            else ErrorCode.UNKNOWN
        )
        message = str(e) or "Unknown error"

        # 瞬态可重试错误且未耗尽 retry_count → 状态回 QUEUED 并重入队（M7）
        if is_retryable(code) and _schedule_retry(db, task, code, message):
            return {"status": "RETRY"}

        _fail(db, task, code, message, lang)
        return {"status": TaskStatus.FAILED}


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _update_status_message(db, task: Task, lang: str) -> None:
    if not task.status_message_id:
        return
    key = _STATUS_I18N.get(TaskStatus(task.status), "status.queued")
    text = t(lang, "task.processing", task_id=task.id, platform=task.platform or "-", status=t(lang, key))
    asyncio.run(delivery.edit_message(task.chat_id, task.status_message_id, text))


async def _upload_all(db, task: Task, result) -> tuple[dict[str, str], list[str]]:
    """上传各产出文件，落库 files 表。

    超过 Telegram Bot API 50MB 上限的文件直接跳过（Bot 端上传必失败），
    返回 ({type: file_id}, [跳过的文件名])。
    """
    max_bytes = get_settings().telegram_max_file_mb * 1024 * 1024
    uploaded: dict[str, str] = {}
    skipped: list[str] = []

    async def _upload(file_type: FileType, path, caption: str | None = None) -> None:
        if path.stat().st_size > max_bytes:
            skipped.append(path.name)
            return
        file_id = await delivery.send_document(task.chat_id, path, caption=caption)
        db.add(File(
            task_id=task.id,
            user_id=task.user_id,
            type=file_type.value,
            filename=path.name,
            size=path.stat().st_size,
            local_path=str(path),
            telegram_file_id=file_id,
        ))
        uploaded[file_type.value] = file_id

    if result.pdf_path is not None:
        await _upload(FileType.PDF, result.pdf_path)
    if result.markdown_path is not None:
        await _upload(FileType.MARKDOWN, result.markdown_path)
    zip_path = result.task_dir / "images.zip"
    if result.images and zip_path.exists():
        await _upload(FileType.IMAGES_ZIP, zip_path)
    db.flush()
    return uploaded, skipped


def _completion_text(task: Task, lang: str) -> str:
    """完成消息：标题 + 3 行原文 + 原始链接 + 产出清单（规格 §9）。"""
    from app.database.enums import FileType

    outputs = []
    if any(f.type == FileType.PDF.value for f in task.files):
        outputs.append(t(lang, "archive.output_pdf"))
    if any(f.type == FileType.MARKDOWN.value for f in task.files):
        outputs.append(t(lang, "archive.output_markdown"))
    return t(
        lang,
        "archive.completed",
        title=task.title or "Untitled",
        source=task.platform or "-",
        author=task.author or "-",
        published=task.published_at or "-",
        excerpt=task.excerpt or "",
        url=task.url,
        outputs=" · ".join(outputs),
    )


def _schedule_retry(db, task: Task, code: str, message: str) -> bool:
    """可重试失败按配置重入队；返回 False 表示次数已耗尽需 FAILED。

    把本次计数写入 ``Task.retry_count`` 并把状态回退到 QUEUED，保证重试 job
    通过 ``_process`` 的状态机幂等门槛（status != QUEUED 会跳过），随后以
    唯一 job id（``enqueue_task_retry``）重入队。
    """
    settings = get_settings()
    attempts = (task.retry_count or 0) + 1
    if attempts > settings.retry_count:
        return False
    task.retry_count = attempts
    task.error_code = code
    task.error_message = message
    task_manager.set_status(db, task, TaskStatus.QUEUED)
    db.commit()
    enqueue_task_retry(task.id, attempts)
    logger.info("task %s retrying, attempt %s/%s (code=%s)", task.id, attempts, settings.retry_count, code)
    return True


def _fail(db, task: Task, code: str, message: str, lang: str) -> None:
    task.error_code = code
    task.error_message = message
    task_manager.set_status(db, task, TaskStatus.FAILED)
    audit(db, action=AuditAction.TASK_FAILED, operator_user_id=task.user_id,
          target_type="task", target_id=task.id, details={"code": code})
    db.commit()
    try:
        reason = _error_text(code, lang)
        asyncio.run(delivery.send_message(
            task.chat_id,
            t(lang, "archive.failed", reason=reason, platform=task.platform or "-", url=task.url),
        ))
    except Exception:  # noqa: BLE001
        logger.exception("failed to send failure message for task %s", task.id)


def _error_text(code: str, lang: str) -> str:
    mapping = {
        ErrorCode.LOGIN_REQUIRED: "error.login_required",
        ErrorCode.INVALID_URL: "error.invalid_url",
        ErrorCode.NOT_FOUND: "error.not_found",
        ErrorCode.HTTP_FORBIDDEN: "error.forbidden",
        ErrorCode.TIMEOUT: "error.timeout",
        ErrorCode.EMPTY_CONTENT: "error.empty_content",
        ErrorCode.IMAGE_DOWNLOAD_FAILED: "error.image_download_failed",
        ErrorCode.PDF_GENERATION_FAILED: "error.pdf_generation_failed",
        ErrorCode.TELEGRAM_UPLOAD_FAILED: "error.telegram_upload_failed",
        ErrorCode.STORAGE_FULL: "error.storage_full",
    }
    return t(lang, mapping.get(code, "error.unknown"))


def _cleanup_local(db, task: Task) -> None:
    """上传成功后：本地文件可删，DB 保留 file_id（规格 §13/§37）。"""
    storage = get_storage()
    if task.storage_uuid:
        storage.delete_task(task.storage_uuid)
    for f in task.files:
        f.deleted_at = datetime.now(timezone.utc)
    db.commit()


def _ensure_soft_limit_cleanup(db, current_uuid: str | None) -> None:
    """任务完成后：超过软限则后台清理到 target（M5 遗留接线）。

    保护运行中任务与当前任务目录（cleanup_if_needed 内部从 DB 取运行中
    目录并追加 current_uuid）。清理失败不影响任务完成状态（只记日志）。
    """
    try:
        cleanup_if_needed(db, current_uuid=current_uuid)
    except Exception:  # noqa: BLE001
        logger.exception("soft-limit storage cleanup failed")
