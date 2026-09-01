"""Web Admin 认证（规格 §41/§50）：Session 登录 + 登录审计。

MVP：单账号密码登录（WEB_ADMIN_PASSWORD），会话用 itsdangerous 签名 Cookie。
完整 RBAC（按 ADMIN_IDS 身份登录）在 M6 阶段扩展。
"""

import logging
from functools import wraps

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings
from app.database.database import SessionLocal
from app.database.enums import AuditAction
from app.database.services import audit

logger = logging.getLogger(__name__)

SESSION_COOKIE = "admin_session"
SESSION_MAX_AGE = 86400  # 24h


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.web_admin_secret, salt="web-admin")


def create_session() -> str:
    return _serializer().dumps({"admin": True})


def read_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return False
    return bool(data.get("admin"))


def login_required(view):
    @wraps(view)
    async def wrapper(request: Request, *args, **kwargs):
        if not read_session(request):
            return RedirectResponse("/admin/login", status_code=303)
        return await view(request, *args, **kwargs)

    return wrapper


def verify_password(password: str) -> bool:
    from secrets import compare_digest

    expected = get_settings().web_admin_password
    return compare_digest(password, expected)


def log_login(request: Request, success: bool, *, reason: str | None = None) -> None:
    db = SessionLocal()
    try:
        details = {"success": success, "ip": request.client.host if request.client else None}
        if reason:
            details["reason"] = reason
        audit(
            db,
            action=AuditAction.ADMIN_LOGIN,
            target_type="web_admin",
            details=details,
        )
        db.commit()
    finally:
        db.close()
