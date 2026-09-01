"""Web Admin CSRF 防护测试（M6 遗留）。

验收：无 csrf_token / 错误 token 的 POST 返回 403；正确 token 通过；
不破坏现有 itsdangerous 登录会话。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.admin.auth as auth_mod
import app.admin.routes as routes_mod
from app.admin.routes import router
from app.database.models import Base, User
from app.database.services import create_user

# 测试用密码来自 config 默认值（conftest 未覆盖 → "change-me"）
PASSWORD = "change-me"


@pytest.fixture()
def client(tmp_path):
    """构建 admin 应用，DB 用临时文件（避开 :memory: 每连接独立的问题）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'admin.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    application = FastAPI()
    application.include_router(router)

    # 覆盖 routes/auth 中的 SessionLocal，指向临时文件 DB
    orig_routes, orig_auth = routes_mod.SessionLocal, auth_mod.SessionLocal
    routes_mod.SessionLocal = session_factory
    auth_mod.SessionLocal = session_factory

    try:
        yield TestClient(application)
    finally:
        routes_mod.SessionLocal = orig_routes
        auth_mod.SessionLocal = orig_auth


def _login(client: TestClient) -> str:
    """GET /admin/login 拿 CSRF token（cookie 被 TestClient 保存）。"""
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "admin_csrf" in resp.cookies
    return client.cookies["admin_csrf"]


def _do_login(client: TestClient) -> str:
    token = _login(client)
    resp = client.post("/admin/login", data={"password": PASSWORD, "csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    return token


def test_login_no_token_returns_403(client):
    resp = client.post("/admin/login", data={"password": PASSWORD})
    assert resp.status_code == 403


def test_login_wrong_token_returns_403(client):
    _login(client)
    resp = client.post("/admin/login", data={"password": PASSWORD, "csrf_token": "forged"})
    assert resp.status_code == 403


def test_login_correct_token_succeeds(client):
    _do_login(client)
    assert "admin_session" in client.cookies


def test_login_wrong_password_with_token_not_403(client):
    """正确 token + 错误密码 → 回登录页(200)，不被 403 误伤。"""
    token = _login(client)
    resp = client.post("/admin/login", data={"password": "wrong", "csrf_token": token})
    assert resp.status_code == 200
    assert "Invalid password" in resp.text


def test_user_action_no_session_no_token_403(client):
    """无 session 无 token：CSRF guard 先于认证拦截 → 403（符合规格'无 token 返回 403'）。"""
    resp = client.post("/admin/users/1/action", data={"action": "approve"}, follow_redirects=False)
    assert resp.status_code == 403


def test_user_action_with_session_but_no_token_403(client):
    _do_login(client)
    resp = client.post("/admin/users/1/action", data={"action": "approve"}, follow_redirects=False)
    assert resp.status_code == 403


def test_user_action_with_session_and_correct_token_303(client):
    """带 session + 正确 csrf → 通过校验走进路由（目标用户不存在→303 跳回 users）。"""
    token = _do_login(client)
    resp = client.post(
        "/admin/users/999/action", data={"action": "approve", "csrf_token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/admin/users")


def test_user_action_actually_changes_db(client):
    """与 routes/auth 相同的临时 DB：approve 真正生效（端到端受保护操作）。"""
    token = _do_login(client)

    db = routes_mod.SessionLocal()
    u = create_user(db, telegram_id=777, username="to-approve")
    db.commit()
    uid = u.id
    db.close()

    resp = client.post(
        f"/admin/users/{uid}/action", data={"action": "approve", "csrf_token": token}, follow_redirects=False
    )
    assert resp.status_code == 303

    db = routes_mod.SessionLocal()
    assert db.get(User, uid).status == "ACTIVE"
    db.close()


def test_logout_keeps_csrf_reissuable(client):
    """登出后仍可从登录页重新取 token，CSRF 与登录会话相互独立。"""
    _do_login(client)

    resp = client.get("/admin/logout", follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert "admin_csrf" in resp.cookies
