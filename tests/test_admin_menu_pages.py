"""管理中心菜单测试：Cookie 管理页（adm:cookies）。

weread 相关入口已随订阅跟踪功能撤销移除（见 docs/02 ADR-12）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.bot.handlers.admin as admin_mod
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
        self.edits = []

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
    return db_factory


@pytest.fixture()
def admin_user(db_factory):
    db = db_factory()
    create_user(db, telegram_id=888001, role="ADMIN", status="ACTIVE")
    db.commit()
    db.close()
    return _FakeUser(888001)


def test_admin_menu_has_cookies_entry():
    kb = admin_menu("zh-CN")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:cookies" in data
    assert "adm:weread" not in data  # 订阅跟踪已撤销


async def test_adm_cookies_page(patch_session, admin_user):
    cb = _FakeCallback(_FakeUser(admin_user.id), "adm:cookies")
    await admin_mod.cookies_page(cb)
    text = cb.message.edits[-1]["text"]
    assert "Cookie" in text


async def test_adm_cookies_denied_for_non_admin(patch_session, db_factory):
    db = db_factory()
    create_user(db, telegram_id=888777, status="ACTIVE")
    db.commit()
    db.close()
    cb = _FakeCallback(_FakeUser(888777), "adm:cookies")
    await admin_mod.cookies_page(cb)
    assert any(a.get("show_alert") for a in cb.answers)
