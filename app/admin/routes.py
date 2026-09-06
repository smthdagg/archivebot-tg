"""Web Admin 路由（规格 §41-§44）：Dashboard / Users / Pending / Tasks / Logs。"""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.admin.auth import create_session, log_login, login_required, read_session, verify_password
from app.admin.csrf import csrf_guard, generate_csrf_token, set_csrf_on_response
from app.admin.ratelimit import login_throttle
from app.database.database import SessionLocal
from app.database.enums import AuditAction
from app.database.models import AuditLog, SystemSetting, Task, User
from app.database.services import audit
from app.storage.manager import get_storage
from app.tasks.queue import queue_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/admin/templates")

LoginLockedMessage = "Too many failed attempts. Please try again later."


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _audit_lockout(request: Request, ip: str) -> None:
    """锁定事件落库（规格 §50 审计要求）。"""
    db = SessionLocal()
    try:
        audit(
            db,
            action=AuditAction.ADMIN_LOCKOUT,
            target_type="web_admin",
            details={"ip": ip, "client_host": request.client.host if request.client else None},
        )
        db.commit()
    finally:
        db.close()


def _render(request: Request, name: str, **ctx):
    """渲染模板：注入 CSRF token 并写入 CSRF cookie（double-submit）。

    登录页与受保护页面共用，保证 POST 表单都能拿到与 cookie 一致的 token。
    """
    token = generate_csrf_token()
    response = templates.TemplateResponse(
        request, name, {**ctx, "csrf_token": token, "request": request}
    )
    set_csrf_on_response(response, token)
    return response


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if read_session(request):
        return RedirectResponse("/admin/", status_code=303)
    ip = _client_ip(request)
    if login_throttle.is_blocked(ip):
        return _render(request, "login.html", error=LoginLockedMessage)
    return _render(request, "login.html", error=None)


@router.post("/login")
async def login_submit(request: Request, _: None = Depends(csrf_guard), password: str = Form(...)):
    ip = _client_ip(request)
    if login_throttle.is_blocked(ip):
        log_login(request, False, reason="locked")
        return _render(request, "login.html", error=LoginLockedMessage)
    ok = verify_password(password)
    if not ok:
        triggered = login_throttle.record_failure(ip)
        log_login(request, False, reason="lockout" if triggered else None)
        if triggered:
            _audit_lockout(request, ip)
        return _render(request, "login.html", error="Invalid password")
    login_throttle.record_success(ip)
    log_login(request, True)
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie("admin_session", create_session(), max_age=86400, httponly=True, samesite="lax")
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


# ---------------------------------------------------------------------------
# Dashboard（规格 §41）
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
@login_required
async def dashboard(request: Request):
    db = SessionLocal()
    try:
        storage = get_storage()
        today = func.date(Task.created_at)
        ctx = {
            "page": "dashboard",
            "users_total": db.scalar(select(func.count(User.id))) or 0,
            "users_pending": db.scalar(select(func.count(User.id)).where(User.status == "PENDING")) or 0,
            "users_active": db.scalar(select(func.count(User.id)).where(User.status == "ACTIVE")) or 0,
            "tasks_today": db.scalar(select(func.count(Task.id)).where(today == func.date("now"))) or 0,
            "tasks_failed": db.scalar(select(func.count(Task.id)).where(Task.status == "FAILED")) or 0,
            "storage_mb": int(storage.total_size() / (1024 * 1024)),
            "storage_limit_mb": int(storage.hard_limit / (1024 * 1024)),
            "queue": queue_stats(),
        }
        return _render(request, "dashboard.html", **ctx)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 用户（规格 §42）
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
@login_required
async def users(request: Request, status: str = ""):
    db = SessionLocal()
    try:
        stmt = select(User).order_by(User.created_at.desc()).limit(100)
        if status:
            stmt = stmt.where(User.status == status.upper())
        rows = list(db.scalars(stmt))
        return _render(request, "users.html", page="users", users=rows, status=status)
    finally:
        db.close()


@router.post("/users/{user_id}/action")
@login_required
async def user_action(request: Request, user_id: int, _: None = Depends(csrf_guard), action: str = Form(...)):
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        from app.database.enums import AuditAction, UserStatus

        if action == "approve":
            user.status = UserStatus.ACTIVE
            audit(db, action=AuditAction.USER_APPROVE, target_type="user", target_id=user.id)
        elif action == "disable":
            user.status = UserStatus.DISABLED
            audit(db, action=AuditAction.USER_DISABLE, target_type="user", target_id=user.id)
        elif action == "enable":
            user.status = UserStatus.ACTIVE
            audit(db, action=AuditAction.USER_ENABLED, target_type="user", target_id=user.id)
        elif action == "delete":
            user.status = UserStatus.DELETED
            audit(db, action=AuditAction.USER_DELETE, target_type="user", target_id=user.id)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# 任务（规格 §43）
# ---------------------------------------------------------------------------

@router.get("/tasks", response_class=HTMLResponse)
@login_required
async def tasks(request: Request):
    db = SessionLocal()
    try:
        rows = list(db.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
        return _render(request, "tasks.html", page="tasks", tasks=rows)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cookies（特殊网站 Cookie 列表 + 到期时间，Bot/Web 全生命周期）
# ---------------------------------------------------------------------------

def _cookie_banner() -> str | None:
    try:
        from app.archive.cookie_expiry import admin_cookie_list_items
        items = admin_cookie_list_items()
        expiring = [
            f"{it['display_name']}（{it['days_left']} 天）"
            for it in items if it["status"] in ("expiring", "expired")
        ]
        if expiring:
            return "⚠️ Cookie 将过期/已过期：" + "、".join(expiring) + "，请到 Cookies 页更新"
    except Exception:
        pass
    return None


@router.get("/settings", response_class=HTMLResponse)
@login_required
async def settings_page(request: Request):

    db = SessionLocal()
    try:
        row = db.get(SystemSetting, "registration_code")
        code = (row.value if row else "") or ""
        return _render(request, "settings.html", page="settings",
                       registration_code=code,
                       regcode_updated=row.updated_at.strftime("%Y-%m-%d %H:%M") if row and row.updated_at else None,
                       regcode_updated_by=row.updated_by if row else None,
                       saved=False)
    finally:
        db.close()


@router.post("/settings/registration-code")
@login_required
async def settings_save_regcode(request: Request, _: None = Depends(csrf_guard)):
    from app.database.services import set_setting

    form = await request.form()
    code = str(form.get("registration_code") or "").strip()[:64]
    db = SessionLocal()
    try:
        # 单账号 Session（无具体管理员 ID），updated_by 置空
        set_setting(db, "registration_code", code, operator_user_id=None)
        db.commit()
        row = db.get(SystemSetting, "registration_code")
        return _render(request, "settings.html", page="settings",
                       registration_code=code,
                       regcode_updated=row.updated_at.strftime("%Y-%m-%d %H:%M") if row and row.updated_at else None,
                       regcode_updated_by=row.updated_by if row else None,
                       saved=True)
    finally:
        db.close()


@router.get("/cookies", response_class=HTMLResponse)
@login_required
async def cookies_page(request: Request):
    from app.archive.cookie_expiry import admin_cookie_list_items
    items = admin_cookie_list_items()
    banner = _cookie_banner()
    return _render(request, "cookies.html", page="cookies", items=items, banner=banner)


@router.post("/cookies/{site_key}/update")
@login_required
async def cookies_update(request: Request, site_key: str, _: None = Depends(csrf_guard)):
    site_key = site_key.strip().lower()
    from app.archive.cookie_parser import parse_netscape
    from app.archive.cookie_registry import SPECIAL_SITES

    if site_key not in SPECIAL_SITES:
        return RedirectResponse("/admin/cookies", status_code=303)
    form = await request.form()
    file = form.get("cookies_file")
    raw_text = ""
    if file is not None and hasattr(file, "read"):
        try:
            raw_text = (await file.read()).decode("utf-8", errors="ignore")
        except Exception:
            raw_text = ""
    if not raw_text or not raw_text.strip():
        raw_text = str(form.get("cookies_text") or "")
    if not raw_text or not raw_text.strip():
        return RedirectResponse("/admin/cookies", status_code=303)
    domains = SPECIAL_SITES[site_key].get("domains", [])
    cookies = parse_netscape(raw_text, domains)
    if not cookies:
        # 尝试通用 Cookie: 头
        text = raw_text.strip()
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1].strip()
        for part in text.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if name and value:
                cookies.append({
                    "name": name, "value": value,
                    "domain": domains[0] if domains else ".example.com",
                    "path": "/",
                })
    if not cookies:
        return RedirectResponse("/admin/cookies", status_code=303)
    import json as _json
    from pathlib import Path as _Path

    from app.config import get_settings as _gs

    settings = _gs()
    path_str = settings.cookie_profiles_file or "data/cookie_profiles.json"
    path = _Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    platform = SPECIAL_SITES[site_key].get("platform", site_key)
    if site_key not in data:
        data[site_key] = {}
    data[site_key][platform] = cookies
    path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if not settings.cookie_profiles_file:
        env_path = _Path(".env")
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            found = False
            out = []
            for line in lines:
                if line.startswith("COOKIE_PROFILES_FILE="):
                    out.append(f"COOKIE_PROFILES_FILE={path_str}")
                    found = True
                else:
                    out.append(line)
            if not found:
                out.append(f"COOKIE_PROFILES_FILE={path_str}")
            env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        _gs.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    db = SessionLocal()
    try:
        from app.database.enums import AuditAction
        audit(db, action=AuditAction.COOKIE_UPDATED, operator_user_id=None,
              target_type="cookie_site", target_id=site_key,
              details={"count": len(cookies), "site_key": site_key})
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/cookies", status_code=303)


# ---------------------------------------------------------------------------
# 日志（规格 §44）
# ---------------------------------------------------------------------------

@router.get("/logs", response_class=HTMLResponse)
@login_required
async def logs(request: Request):
    db = SessionLocal()
    try:
        rows = list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)))
        return _render(request, "logs.html", page="logs", logs=rows)
    finally:
        db.close()
