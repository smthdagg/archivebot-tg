"""管理中心菜单入口测试：Cookie/微信读书页面 + 扫码登录按钮（stub，不触网）。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.archive.weread_client as weread_client_mod
import app.bot.handlers.admin as admin_mod
import app.bot.handlers.subscribe as subscribe_mod
from app.bot.keyboards import admin_menu
from app.database.models import Base
from app.database.services import create_user


class _FakeUser:
    def __init__(self, uid, language_code="en"):
        self.id = uid
        self.language_code = language_code


class _FakeMessage:
    def __init__(self, user, chat_id=777):
        self.from_user = user
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = 9
        self.answers = []
        self.edits = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append({"text": text, "reply_markup": reply_markup})
        return self

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append({"text": text, "reply_markup": reply_markup})


class _FakeCallback:
    def __init__(self, user, data, message=None):
        self.from_user = user
        self.data = data
        self.message = message or _FakeMessage(user)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})


@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'menu.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    yield sf
    engine.dispose()


@pytest.fixture()
def patch_session(db_factory, monkeypatch):
    monkeypatch.setattr(admin_mod, "SessionLocal", db_factory)
    monkeypatch.setattr(subscribe_mod, "SessionLocal", db_factory)
    return db_factory


@pytest.fixture()
def admin_user(db_factory):
    db = db_factory()
    create_user(db, telegram_id=888001, role="ADMIN", status="ACTIVE")
    db.commit()
    db.close()
    return _FakeUser(888001)


def test_admin_menu_contains_new_entries():
    kb = admin_menu("zh-CN")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:cookies" in data and "adm:weread" in data


async def test_adm_weread_denied_for_non_admin(patch_session, db_factory):
    db = db_factory()
    create_user(db, telegram_id=888777, status="ACTIVE")
    db.commit()
    db.close()
    cb = _FakeCallback(_FakeUser(888777), "adm:weread")
    await admin_mod.weread_page(cb)
    assert any(a.get("show_alert") for a in cb.answers)


async def test_adm_weread_shows_login_button(patch_session, admin_user, monkeypatch):
    async def fake_call(op, *args, **kwargs):
        return {"ok": True, "account": "default"}

    monkeypatch.setattr(weread_client_mod, "call_async", fake_call)
    cb = _FakeCallback(_FakeUser(admin_user.id), "adm:weread")
    await admin_mod.weread_page(cb)
    markup = str(cb.message.edits[-1]["reply_markup"])
    assert "wlogin" in markup
    assert "📡" in cb.message.edits[-1]["text"] or "WeChat Read" in cb.message.edits[-1]["text"]


async def test_adm_cookies_page(patch_session, admin_user):
    cb = _FakeCallback(_FakeUser(admin_user.id), "adm:cookies")
    await admin_mod.cookies_page(cb)
    text = cb.message.edits[-1]["text"]
    assert "Cookie" in text


async def test_wlogin_button_runs_login_flow(patch_session, admin_user, monkeypatch):
    events = [
        {"event": "qr", "url": "https://example.com/qr"},
        {"event": "done", "account": "default", "vid": 1},
    ]

    async def fake_login_events(timeout=None):
        for ev in events:
            yield ev

    async def fake_call(op, *args, **kwargs):
        return {"ok": True, "account": "default"}

    monkeypatch.setattr(weread_client_mod, "login_events_async", fake_login_events)
    monkeypatch.setattr(weread_client_mod, "call_async", fake_call)

    msg = _FakeMessage(_FakeUser(admin_user.id))
    cb = _FakeCallback(_FakeUser(admin_user.id), "wlogin", message=msg)
    await subscribe_mod.on_weread_login_button(cb)

    texts = [e["text"] for e in msg.edits]
    assert any("已登录" in t or "logged in" in t for t in texts)


async def test_wlogin_in_progress_guard(patch_session, admin_user, monkeypatch):
    monkeypatch.setattr(subscribe_mod, "_LOGIN_IN_PROGRESS", True)
    msg = _FakeMessage(_FakeUser(admin_user.id))
    cb = _FakeCallback(_FakeUser(admin_user.id), "wlogin", message=msg)
    await subscribe_mod.on_weread_login_button(cb)
    assert any("进行中" in str(a["text"]) or "in progress" in str(a["text"]) for a in msg.answers)

