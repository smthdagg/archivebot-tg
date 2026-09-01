"""通用数据访问：用户服务、审计日志等（供 bot/worker/api 复用）。"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.enums import AuditAction, UserRole, UserStatus
from app.database.models import AuditLog, User


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------

def get_user_by_telegram_id(db: Session, telegram_id: int) -> User | None:
    return db.scalar(select(User).where(User.telegram_id == telegram_id))


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def create_user(
    db: Session,
    *,
    telegram_id: int,
    username: str | None = None,
    display_name: str | None = None,
    language: str = "auto",
    role: str = UserRole.USER,
    status: str = UserStatus.PENDING,
) -> User:
    user = User(
        telegram_id=telegram_id,
        username=username,
        display_name=display_name,
        language=language,
        role=role,
        status=status,
    )
    db.add(user)
    db.flush()
    return user


def touch_user(db: Session, user: User) -> None:
    user.last_active_at = now_utc()
    db.add(user)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def audit(
    db: Session,
    *,
    action: AuditAction | str,
    operator_user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            operator_user_id=operator_user_id,
            action=str(action),
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details,
        )
    )
    db.flush()
