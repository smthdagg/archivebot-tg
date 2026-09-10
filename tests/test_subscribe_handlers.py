"""公众号订阅 handler 单元测试（stub aiogram 对象与 weread 桥接，不触网）。

覆盖：/subscribe 搜索→选号→交付方式（notify/auto）→ 落库；登录缺失提示；
每用户上限；退订所有权与最后订阅者的服务端退订；通知按钮所有权 + FSM 交接；
/weread_status 管理员门禁。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.bot.handlers.subscribe as subscribe_mod
from app.archive.weread_client import WereadError, WereadTokenExpired
from app.database.enums import DeliveryMode, UserStatus
from app.database.models import Base, WxMpAccount, WxPendingArticle, WxSubscription
from app.database.services import create_user


class _FakeUser:
    def __init__(self, uid, language_code="en"):
        self.id = uid
        self.language_code = language_code


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, user, text="", chat_id=123456, message_id=7):
        self.from_user = user
        self.text = text
        self.chat = _FakeChat(chat_id)
        self.message_id = message_id
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


class _FakeFSM:
    def __init__(self):
        self.data = {}
        self.state = None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state


@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'subscribe.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    yield sf
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def patch_session(db_factory, monkeypatch):
    monkeypatch.setattr(subscribe_mod, "SessionLocal", db_factory)
    return db_factory


@pytest.fixture()
def user(db):
    u = create_user(db, telegram_id=555001, username="subber", language="zh-CN",
                    status=UserStatus.ACTIVE)
    db.commit()
    return u


@pytest.fixture()
def fake_user():
    return _FakeUser(555001)


@pytest.fixture()
def stub_search(monkeypatch):
    """weread_client.call_async 替身：按 op 分发，可注入错误。"""
    state: dict[str, Any] = {"search": {"results": [
        {"account_id": "MP_WXS_111", "title": "测试号", "author": "a", "cover": ""},
    ]}, "subscribe_error": None, "subscribe_calls": []}

    async def fake_call(op, *args, **kwargs):
        if op == "search":
            if isinstance(state["search"], Exception):
                raise state["search"]
            return state["search"]
        if op == "subscribe":
            state["subscribe_calls"].append(args[0])
            if state["subscribe_error"]:
                raise state["subscribe_error"]
            return {"ok": True}
        if op == "unsubscribe":
            return {"ok": True}
        if op == "status":
            return {"ok": True, "account": "default"}
        raise AssertionError(f"unexpected op {op}")

    monkeypatch.setattr(subscribe_mod.weread_client, "call_async", fake_call)
    return state


# ---------------------------------------------------------------------------
# /subscribe 搜索流程
# ---------------------------------------------------------------------------

async def test_subscribe_search_lists_candidates(patch_session, db, user, fake_user,
                                                 stub_search):
    msg = _FakeMessage(fake_user, text="/subscribe 测试号")
    await subscribe_mod.on_subscribe(msg)
    assert any("MP_WXS_111" in str(a["reply_markup"]) for a in msg.answers)


async def test_subscribe_search_no_result(patch_session, db, user, fake_user, stub_search):
    stub_search["search"] = {"results": []}
    msg = _FakeMessage(fake_user, text="/subscribe 不存在的号")
    await subscribe_mod.on_subscribe(msg)
    assert any("未找到" in a["text"] or "No matching" in a["text"] for a in msg.answers)


async def test_subscribe_search_not_logged_in(patch_session, db, user, fake_user, stub_search):
    stub_search["search"] = WereadTokenExpired("登录超时", code=-2012)
    msg = _FakeMessage(fake_user, text="/subscribe 测试号")
    await subscribe_mod.on_subscribe(msg)
    assert any("/weread_login" in a["text"] for a in msg.answers)


async def test_subscribe_url_without_nickname_asks_name(patch_session, db, user, fake_user,
                                                        monkeypatch):
    monkeypatch.setattr(subscribe_mod, "_nickname_from_article", lambda url: None)
    msg = _FakeMessage(fake_user, text="/subscribe https://mp.weixin.qq.com/s/xyz")
    await subscribe_mod.on_subscribe(msg)
    assert any("name_needed" in a["text"] or "无法从链接" in a["text"] for a in msg.answers)


# ---------------------------------------------------------------------------
# 选号 → 交付方式 → 落库
# ---------------------------------------------------------------------------

async def test_pick_then_notify_creates_subscription(patch_session, db, user, fake_user,
                                                     stub_search):
    pick = _FakeCallback(fake_user, "wsubpick:MP_WXS_111")
    await subscribe_mod.on_pick(pick)
    assert pick.message.edits and "wsubmode" in str(pick.message.edits[0]["reply_markup"])

    mode = _FakeCallback(fake_user, "wsubmode:MP_WXS_111:notify")
    await subscribe_mod.on_mode(mode)
    assert stub_search["subscribe_calls"] == ["MP_WXS_111"]
    assert db.query(WxMpAccount).filter_by(account_id="MP_WXS_111").count() == 1
    sub = db.query(WxSubscription).filter_by(user_id=user.id).one()
    assert sub.delivery_mode == DeliveryMode.NOTIFY.value
    assert sub.chat_id == mode.message.chat.id
    assert sub.last_article_time > 0  # 基线＝订阅时刻，不分发历史文章
    assert any("已订阅" in e["text"] or "Subscribed" in e["text"] for e in mode.message.edits)


async def test_pick_then_auto_with_format(patch_session, db, user, fake_user, stub_search):
    await subscribe_mod.on_pick(_FakeCallback(fake_user, "wsubpick:MP_WXS_111"))
    mode = _FakeCallback(fake_user, "wsubmode:MP_WXS_111:auto")
    await subscribe_mod.on_mode(mode)
    assert mode.message.edits and "wsubfmt" in str(mode.message.edits[0]["reply_markup"])

    fmt = _FakeCallback(fake_user, "wsubfmt:MP_WXS_111:md")
    await subscribe_mod.on_fmt(fmt)
    sub = db.query(WxSubscription).one()
    assert sub.delivery_mode == DeliveryMode.AUTO.value
    assert sub.output_types == ["MARKDOWN"]


async def test_need_login_blocks_subscribe(patch_session, db, user, fake_user, stub_search):
    stub_search["subscribe_error"] = WereadTokenExpired("登录超时", code=-2012)
    await subscribe_mod.on_pick(_FakeCallback(fake_user, "wsubpick:MP_WXS_111"))
    cb = _FakeCallback(fake_user, "wsubmode:MP_WXS_111:notify")
    await subscribe_mod.on_mode(cb)
    assert any(cb.answers[i]["text"] and "/weread_login" in str(cb.answers[i]["text"])
               for i in range(len(cb.answers)))
    assert db.query(WxSubscription).count() == 0


async def test_per_user_cap(patch_session, db, user, fake_user, stub_search, monkeypatch):
    settings = subscribe_mod.get_settings()
    monkeypatch.setattr(settings, "wasub_max_subs_per_user", 1)
    await subscribe_mod.on_pick(_FakeCallback(fake_user, "wsubpick:MP_WXS_111"))
    await subscribe_mod.on_mode(_FakeCallback(fake_user, "wsubmode:MP_WXS_111:notify"))
    assert db.query(WxSubscription).count() == 1

    # 第二个号被上限拦截
    stub_search["search"] = {"results": [
        {"account_id": "MP_WXS_222", "title": "另一个号", "author": "", "cover": ""}]}
    await subscribe_mod.on_pick(_FakeCallback(fake_user, "wsubpick:MP_WXS_222"))
    cb2 = _FakeCallback(fake_user, "wsubmode:MP_WXS_222:notify")
    await subscribe_mod.on_mode(cb2)
    assert db.query(WxSubscription).count() == 1
    assert any(cb2.answers[i].get("show_alert") for i in range(len(cb2.answers)))


async def test_unsubscribe_removes_and_resets_account(patch_session, db, user, fake_user,
                                                      stub_search):
    account = WxMpAccount(account_id="MP_WXS_111", mp_name="测试号")
    sub = WxSubscription(user_id=user.id, chat_id=1, account_id="MP_WXS_111",
                         delivery_mode="notify", last_article_time=1)
    db.add_all([account, sub])
    db.commit()
    sub_id = sub.id

    await subscribe_mod.on_unsubscribe(_FakeCallback(fake_user, f"wsubrm:{sub_id}"))

    db.expire_all()
    assert db.get(WxSubscription, sub_id).active is False
    # 最后一个订阅者退订 → 服务端退订 + 账号行停用
    assert db.get(WxMpAccount, account.id).active is False


async def test_unsubscribe_denied_for_other_user(patch_session, db, db_factory, fake_user,
                                                 stub_search):
    other = create_user(db, telegram_id=555999, status=UserStatus.ACTIVE)
    account = WxMpAccount(account_id="MP_WXS_111", mp_name="测试号")
    sub = WxSubscription(user_id=other.id, chat_id=1, account_id="MP_WXS_111",
                         delivery_mode="notify", last_article_time=1)
    db.add_all([account, sub])
    db.commit()

    await subscribe_mod.on_unsubscribe(_FakeCallback(fake_user, f"wsubrm:{sub.id}"))
    assert db.get(WxSubscription, sub.id).active is True


async def test_switch_existing_sub_to_auto(patch_session, db, user, fake_user, stub_search):
    sub = WxSubscription(user_id=user.id, chat_id=1, account_id="MP_WXS_111",
                         delivery_mode="notify", last_article_time=1)
    db.add(sub)
    db.commit()

    await subscribe_mod.on_change_mode(_FakeCallback(fake_user, f"wsubchg:{sub.id}:auto"))
    # 切 auto 先进格式选择
    fmt = _FakeCallback(fake_user, f"wsubfmt:{sub.id}:pdf")
    await subscribe_mod.on_fmt(fmt)
    db.expire_all()
    updated = db.get(WxSubscription, sub.id)
    assert updated.delivery_mode == DeliveryMode.AUTO.value
    assert updated.output_types == ["PDF"]


# ---------------------------------------------------------------------------
# 通知按钮 → 现有归档 FSM
# ---------------------------------------------------------------------------

async def test_wsubgo_hands_off_to_format_fsm(patch_session, db, user, fake_user):
    account = WxMpAccount(account_id="MP_WXS_111", mp_name="测试号")
    sub = WxSubscription(user_id=user.id, chat_id=1, account_id="MP_WXS_111",
                         delivery_mode="notify", last_article_time=0)
    pending = WxPendingArticle(account_id="MP_WXS_111",
                               doc_url="https://mp.weixin.qq.com/s/ok",
                               title="新文章", article_time=1700000100)
    db.add_all([account, sub, pending])
    db.commit()

    cb = _FakeCallback(fake_user, f"wsubgo:{pending.id}")
    fsm = _FakeFSM()
    await subscribe_mod.on_archive_from_notification(cb, fsm)

    assert fsm.state is not None
    assert fsm.data["pending_platform"] == "wechat"
    assert fsm.data["pending_url"].endswith("/ok")


async def test_wsubgo_denied_without_subscription(patch_session, db, user, fake_user):
    pending = WxPendingArticle(account_id="MP_WXS_111",
                               doc_url="https://mp.weixin.qq.com/s/ok", article_time=1)
    db.add(pending)
    db.commit()
    cb = _FakeCallback(fake_user, f"wsubgo:{pending.id}")
    await subscribe_mod.on_archive_from_notification(cb, _FakeFSM())
    assert any(a.get("show_alert") for a in cb.answers)


async def test_wsubgo_expired_pending(patch_session, db, user, fake_user):
    pending = WxPendingArticle(
        account_id="MP_WXS_111", doc_url="https://mp.weixin.qq.com/s/old", article_time=1,
        created_at=datetime.now(timezone.utc) - timedelta(days=9),
    )
    db.add(pending)
    db.commit()
    WxSubscription(user_id=user.id, chat_id=1, account_id="MP_WXS_111",
                   delivery_mode="notify", last_article_time=0)
    db.commit()
    cb = _FakeCallback(fake_user, f"wsubgo:{pending.id}")
    await subscribe_mod.on_archive_from_notification(cb, _FakeFSM())
    assert any("过期" in str(a["text"]) or "expired" in str(a["text"]) for a in cb.answers)


# ---------------------------------------------------------------------------
# 管理员状态
# ---------------------------------------------------------------------------

async def test_weread_status_admin_gate(patch_session, db, user):
    outsider = _FakeUser(12345)  # 非 ADMIN_IDS
    msg = _FakeMessage(outsider, text="/weread_status")
    await subscribe_mod.weread_status_cmd(msg)
    assert "仅管理员" in msg.answers[0]["text"]


async def test_weread_status_lists_accounts(patch_session, db, user, fake_user, stub_search,
                                            monkeypatch):
    monkeypatch.setattr(subscribe_mod, "_is_admin", lambda uid: True)
    db.add_all([
        WxMpAccount(account_id="MP_WXS_111", mp_name="测试号", last_synckey=42),
        WxSubscription(user_id=user.id, chat_id=1, account_id="MP_WXS_111",
                       delivery_mode="notify", last_article_time=1),
    ])
    db.commit()
    msg = _FakeMessage(fake_user, text="/weread_status")
    await subscribe_mod.weread_status_cmd(msg)
    text = msg.answers[0]["text"]
    assert "测试号" in text and "synckey=42" in text


async def test_generic_error_shows_search_failed(patch_session, db, user, fake_user,
                                                 stub_search):
    stub_search["search"] = WereadError("boom", code=-9999)
    msg = _FakeMessage(fake_user, text="/subscribe 测试号")
    await subscribe_mod.on_subscribe(msg)
    assert any("boom" in a["text"] for a in msg.answers)
