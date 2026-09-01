"""任务管理（设计规格 §29/§30/§35/§54）。

职责：任务 CRUD、状态机、所有权校验、取消、并发限制。
队列与执行见 queue.py / worker.py。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.enums import ErrorCode, TaskStatus
from app.database.models import Task
from app.database.services import audit
from app.storage.manager import get_storage

logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------

def create_task(
    db: Session,
    *,
    user_id: int,
    chat_id: int,
    url: str,
    platform: str,
    output_types: list[str],
    status_message_id: int | None = None,
) -> Task:
    storage = get_storage()
    if not storage.can_accept_new_task():
        raise TaskLimitError(ErrorCode.STORAGE_FULL, "Storage is full")

    task_dir = storage.new_task_dir()
    task = Task(
        user_id=user_id,
        chat_id=chat_id,
        url=url,
        platform=platform,
        status=TaskStatus.QUEUED,
        output_types=output_types,
        storage_uuid=task_dir.name,
        status_message_id=status_message_id,
    )
    db.add(task)
    db.flush()
    audit(db, action="TASK_CREATED", operator_user_id=user_id,
          target_type="task", target_id=task.id, details={"url": url, "platform": platform})
    return task


# ---------------------------------------------------------------------------
# 查询（所有权校验）
# ---------------------------------------------------------------------------

class AccessDeniedError(Exception):
    """当前用户无权访问该任务（规格 §29）。"""


class TaskLimitError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def get_task(db: Session, task_id: int) -> Task | None:
    return db.get(Task, task_id)


def get_task_for_user(db: Session, task_id: int, user_id: int, *, is_admin: bool = False) -> Task:
    """按所有权取任务；非管理员只能访问自己的任务。"""
    task = db.get(Task, task_id)
    if task is None:
        raise AccessDeniedError("task not found")
    if not is_admin and task.user_id != user_id:
        raise AccessDeniedError("access denied")
    return task


def list_user_tasks(db: Session, user_id: int, *, page: int = 1, per_page: int = 5) -> tuple[list[Task], int]:
    stmt = (
        select(Task)
        .where(Task.user_id == user_id)
        .order_by(Task.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    tasks = list(db.scalars(stmt))
    total = db.scalar(
        select(func.count(Task.id)).where(Task.user_id == user_id)
    ) or 0
    return tasks, total


# ---------------------------------------------------------------------------
# 状态机与取消
# ---------------------------------------------------------------------------

def set_status(db: Session, task: Task, status: TaskStatus, *, commit: bool = False) -> None:
    task.status = status.value
    if task.started_at is None and status in (TaskStatus.FETCHING, TaskStatus.PARSING):
        task.started_at = now_utc()
    if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task.completed_at = now_utc()
    db.add(task)
    if commit:
        db.commit()


def request_cancel(db: Session, task: Task) -> None:
    """请求取消：QUEUED 直接取消；运行中置取消标记，worker 检查点终止。"""
    task.cancel_requested = True
    if task.status == TaskStatus.QUEUED:
        task.status = TaskStatus.CANCELLED
    db.add(task)
    audit(db, action="TASK_CANCELLED", operator_user_id=task.user_id,
          target_type="task", target_id=task.id)
    db.commit()


def is_cancelled(task: Task) -> bool:
    return task.cancel_requested or task.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 并发限制
# ---------------------------------------------------------------------------

def user_active_task_count(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count(Task.id)).where(
            Task.user_id == user_id,
            Task.status.in_(TaskStatus.processing_statuses()),
        )
    ) or 0


def global_active_task_count(db: Session) -> int:
    return db.scalar(
        select(func.count(Task.id)).where(Task.status.in_(TaskStatus.processing_statuses()))
    ) or 0
