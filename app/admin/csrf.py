"""Web Admin CSRF 防护（M6 遗留待办，规格 §50）。

采用 itsdangerous 签名 token 的 double-submit 方案：

- GET 渲染（登录页 + 受保护页面）时生成一个签名 token，同时写入
  `admin_csrf` cookie 并在表单注入同名 hidden input。
- 受保护 POST（登录、users/tasks/logs 操作）校验提交的 `csrf_token`
  与 `admin_csrf` cookie 一致且签名有效；缺失或不匹配返回 403。
- 独立于现有 `admin_session`（itsdangerous 登录会话）cookie，不破坏登录态。
"""

import logging
import secrets
from typing import Any

from fastapi import Form, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

logger = logging.getLogger(__name__)

CSRF_COOKIE = "admin_csrf"
CSRF_FIELD = "csrf_token"
CSRF_MAX_AGE = 3600  # 1h


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.web_admin_secret, salt="web-admin-csrf")


def generate_csrf_token() -> str:
    """生成一个签名 CSRF token（payload 含随机 nonce）。"""
    data = {"csrf": secrets.token_urlsafe(32)}
    return _serializer().dumps(data)


def read_csrf_token(request: Request) -> str | None:
    """读取并校验请求携带的签名 CSRF token；无效时返回 None。"""
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired, ValueError):
        return None
    if not isinstance(payload, dict) or "csrf" not in payload:
        return None
    return token


def verify_csrf(request: Request, submitted: str) -> bool:
    """校验提交的表单 token 与有效签名 cookie 是否一致。"""
    expected = read_csrf_token(request)
    if not expected or not submitted:
        return False
    return secrets.compare_digest(expected, submitted)


def csrf_guard(request: Request, csrf_token: str = Form(default="")) -> None:
    """FastAPI 依赖：受保护 POST 的 CSRF 校验，失败抛 403。"""
    if not verify_csrf(request, csrf_token):
        logger.warning("CSRF check failed (missing or mismatched token) for %s", request.url.path)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")


def set_csrf_on_response(response: Any, token: str | None = None) -> str:
    """在响应上写入 CSRF cookie 并返回 token（用于同步注入表单）。"""
    token = token or generate_csrf_token()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=CSRF_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return token
