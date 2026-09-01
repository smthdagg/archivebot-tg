"""Bot handler 公共辅助：用户状态检查与语言解析。"""

from sqlalchemy.orm import Session

from app.bot.i18n import resolve_language
from app.config import get_settings
from app.database.enums import UserRole, UserStatus
from app.database.models import User
from app.database.services import create_user, get_user_by_telegram_id, touch_user


def user_language(user: User | None, telegram_lang: str | None = None) -> str:
    """用户语言：users.language（含 auto）→ Telegram language_code → 默认。"""
    preferred = user.language if user else get_settings().default_language
    return resolve_language(preferred, telegram_lang)


def ensure_user(
    db: Session,
    telegram_id: int,
    username: str | None,
    display_name: str | None,
    telegram_lang: str | None,
) -> tuple[User, bool]:
    """取或创建用户。返回 (user, is_new)。

    创建时：ADMIN_IDS 白名单内 → SUPER_ADMIN + ACTIVE；否则 PENDING 等待审核。
    """
    user = get_user_by_telegram_id(db, telegram_id)
    if user is not None:
        touch_user(db, user)
        db.commit()
        return user, False

    if telegram_id in get_settings().admin_ids:
        user = create_user(
            db,
            telegram_id=telegram_id,
            username=username,
            display_name=display_name,
            language=telegram_lang or "auto",
            role=UserRole.SUPER_ADMIN,
            status=UserStatus.ACTIVE,
        )
    else:
        user = create_user(
            db,
            telegram_id=telegram_id,
            username=username,
            display_name=display_name,
            language=telegram_lang or "auto",
        )
    db.commit()
    return user, True
