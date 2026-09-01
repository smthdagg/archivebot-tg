"""Web Admin 路由（规格 §41-§44）：Dashboard / Users / Pending / Tasks / Logs。"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.admin.auth import create_session, log_login, login_required, read_session, verify_password
from app.database.database import SessionLocal
from app.database.models import AuditLog, Task, User
from app.database.services import audit
from app.storage.manager import get_storage
from app.tasks.queue import queue_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/admin/templates")


def _render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if read_session(request):
        return RedirectResponse("/admin/", status_code=303)
    return _render(request, "login.html", error=None)


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    ok = verify_password(password)
    log_login(request, ok)
    if not ok:
        return _render(request, "login.html", error="Invalid password")
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
            "request": request,
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
async def user_action(request: Request, user_id: int, action: str = Form(...)):
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
