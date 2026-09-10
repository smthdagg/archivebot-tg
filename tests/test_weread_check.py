"""公众号订阅增量检查（weread_check）单元测试（stub 桥接 + 独立 SQLite，不触网）。

覆盖：账号 synckey 回存、notify 分发（pending 落库 + 按钮推送 + 基线推进）、
auto 分发（建任务入队 + 容量满不推进基线）、付费文章跳过、重复文章基线去重、
token 失效整轮暂停 + 管理员提醒。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.archive import weread_client
from app.archive.weread_client import WereadError, WereadTokenExpired
from app.database.models import Base, Task, User, WxMpAccount, WxPendingArticle, WxSubscription
from app.tasks import weread_check
from app.tasks.weread_check import run_subscription_check


@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'weread.db'}")
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
    monkeypatch.setattr(weread_check, "SessionLocal", db_factory)
    return db_factory


@pytest.fixture()
def stub_delivery(monkeypatch):
    calls: dict = {"messages": []}

    async def fake_send_message(chat_id, text, reply_markup=None, parse_mode=None):
        calls["messages"].append((chat_id, text))
        return 5000 + len(calls["messages"])

    def fake_run_async(coro):
        import asyncio as _asyncio

        return _asyncio.run(coro)

    import app.bot.delivery as delivery_mod

    monkeypatch.setattr(delivery_mod, "send_message", fake_send_message)
    monkeypatch.setattr(delivery_mod, "run_async", fake_run_async)
    return calls


@pytest.fixture()
def enqueue(monkeypatch):
    calls: list[int] = []

    def fake_enqueue(task_id, **_):
        calls.append(task_id)

    monkeypatch.setattr(weread_check, "enqueue_task", fake_enqueue)
    return calls


@pytest.fixture()
def no_cookie_profile(monkeypatch):
    monkeypatch.setattr(weread_check, "auto_wechat_profile", lambda: None)


_user_seq = {"n": 0}


def _account(db: Any, account_id: str = "MP_WXS_1", *, synckey: int | None = None) -> WxMpAccount:
    row = WxMpAccount(account_id=account_id, mp_name="测试号", last_synckey=synckey)
    db.add(row)
    db.commit()
    return row


def _subscribed_user(db: Any, *, mode: str = "notify", account_id: str = "MP_WXS_1",
                     last_article_time: int = 0, output_types: list | None = None) -> WxSubscription:
    _user_seq["n"] += 1
    user = User(telegram_id=992000 + _user_seq["n"], username="sub", language="zh-CN",
                status="ACTIVE")
    db.add(user)
    db.commit()
    sub = WxSubscription(
        user_id=user.id, chat_id=user.telegram_id, account_id=account_id,
        delivery_mode=mode, output_types=output_types or [], last_article_time=last_article_time,
    )
    db.add(sub)
    db.commit()
    return sub


_PAGE = {
    "account_id": "MP_WXS_1",
    "synckey": 42,
    "has_more": 0,
    "articles": [
        {"review_id": "a1", "title": "文章一", "doc_url": "https://mp.weixin.qq.com/s/a1",
         "article_time": 1700000100, "mp_name": "测试号", "pay_type": 0},
        {"review_id": "a2", "title": "付费文章", "doc_url": "https://mp.weixin.qq.com/s/a2",
         "article_time": 1700000200, "mp_name": "测试号", "pay_type": 2},
        {"review_id": "a3", "title": "无时间", "doc_url": "https://mp.weixin.qq.com/s/a3",
         "article_time": 0, "mp_name": "测试号", "pay_type": 0},
    ],
}


def test_synckey_and_notify_flow(db, patch_session, monkeypatch, stub_delivery, enqueue,
                                 no_cookie_profile):
    account = _account(db)  # 无 synckey → 首拉
    sub = _subscribed_user(db, mode="notify", last_article_time=0)

    seen_opts: list[list[str]] = []

    def fake_call(op, *args, **kwargs):
        assert op == "articles"
        seen_opts.append(list(args))
        return dict(_PAGE)

    monkeypatch.setattr(weread_client, "call_sync", fake_call)
    monkeypatch.setattr(weread_client, "node_available", lambda: True)

    stats = run_subscription_check(db)

    # 首轮无 synckey → 不带 --synckey 参数
    assert "--synckey" not in seen_opts[0]
    assert account.last_synckey == 42  # 游标已回存

    # notify：一条 pending + 一条推送（付费/无时间文章不分发）
    pending = db.query(WxPendingArticle).all()
    assert len(pending) == 1
    assert pending[0].doc_url.endswith("/a1")
    assert len(stub_delivery["messages"]) == 1
    assert "文章一" in stub_delivery["messages"][0][1]
    # 基线推进到最新分发文章
    assert sub.last_article_time == 1700000100
    assert stats["notify"] == 1


def test_baseline_filters_old_articles(db, patch_session, monkeypatch, stub_delivery, enqueue,
                                       no_cookie_profile):
    _account(db, synckey=10)
    sub = _subscribed_user(db, mode="notify", last_article_time=1700000100)
    monkeypatch.setattr(weread_client, "call_sync", lambda *a, **k: dict(_PAGE))
    monkeypatch.setattr(weread_client, "node_available", lambda: True)

    run_subscription_check(db)

    # a1 已过基线不再分发；无新可分发文章 → 无 pending、基线不动
    assert db.query(WxPendingArticle).count() == 0
    assert sub.last_article_time == 1700000100


def test_auto_mode_creates_task(db, patch_session, monkeypatch, stub_delivery, enqueue,
                                no_cookie_profile):
    _account(db, synckey=10)
    sub = _subscribed_user(db, mode="auto", last_article_time=0,
                           output_types=["MARKDOWN"])
    monkeypatch.setattr(weread_client, "call_sync", lambda *a, **k: dict(_PAGE))
    monkeypatch.setattr(weread_client, "node_available", lambda: True)

    stats = run_subscription_check(db)

    assert stats["auto"] == 1
    assert len(enqueue) == 1
    task = db.query(Task).one()
    assert task.url.endswith("/a1")
    assert task.platform == "wechat"
    assert task.output_types == ["MARKDOWN"]
    assert sub.last_article_time == 1700000100


def test_concurrency_full_skips_article(db, patch_session, monkeypatch, stub_delivery, enqueue,
                                        no_cookie_profile):
    """auto 模式用户并发满：不建任务、基线不推进（下轮补发）。"""
    _account(db, synckey=10)
    sub = _subscribed_user(db, mode="auto", last_article_time=0)
    monkeypatch.setattr(weread_client, "call_sync", lambda *a, **k: dict(_PAGE))
    monkeypatch.setattr(weread_client, "node_available", lambda: True)
    monkeypatch.setattr(weread_check, "_user_at_capacity", lambda db_, uid: True)

    stats = run_subscription_check(db)

    assert stats["auto"] == 0
    assert enqueue == []
    assert sub.last_article_time == 0
    assert db.query(Task).count() == 0


def test_token_expired_pauses_cycle_and_notifies(db, patch_session, monkeypatch, stub_delivery,
                                                 enqueue, no_cookie_profile):
    a1 = _account(db, "MP_WXS_1", synckey=5)
    a2 = _account(db, "MP_WXS_2", synckey=5)
    _subscribed_user(db, mode="notify", account_id="MP_WXS_1")
    _subscribed_user(db, mode="notify", account_id="MP_WXS_2")

    calls: list[str] = []

    def fake_call(op, *args, **kwargs):
        calls.append(args[0])
        raise WereadTokenExpired("登录超时", code=-2012)

    notified: list[tuple[str, str]] = []

    def fake_notify(kind, text):
        notified.append((kind, text))

    monkeypatch.setattr(weread_client, "call_sync", fake_call)
    monkeypatch.setattr(weread_client, "node_available", lambda: True)
    monkeypatch.setattr(weread_check, "notify_admins", fake_notify)

    stats = run_subscription_check(db)

    # 第一个账号失败即整轮暂停（不再请求第二个账号）
    assert calls == ["MP_WXS_1"]
    assert stats["errors"] == 0
    assert notified and notified[0][0] == "token"
    # 失败账号的 synckey 保持不变（未确认拉取成功不推进）
    assert a1.last_synckey == 5
    assert a2.last_synckey == 5


def test_generic_account_error_continues(db, patch_session, monkeypatch, stub_delivery, enqueue,
                                         no_cookie_profile):
    _account(db, "MP_WXS_1", synckey=5)
    _account(db, "MP_WXS_2", synckey=5)
    _subscribed_user(db, mode="notify", account_id="MP_WXS_2", last_article_time=0)

    calls: list[str] = []

    def fake_call(op, *args, **kwargs):
        calls.append(args[0])
        if args[0] == "MP_WXS_1":
            raise WereadError("boom", code=-9999)
        return dict(_PAGE, account_id="MP_WXS_2", articles=[
            {"review_id": "b1", "title": "二号号文章", "doc_url": "https://mp.weixin.qq.com/s/b1",
             "article_time": 1700000100, "mp_name": "测试号", "pay_type": 0},
        ])

    monkeypatch.setattr(weread_client, "call_sync", fake_call)
    monkeypatch.setattr(weread_client, "node_available", lambda: True)

    stats = run_subscription_check(db)

    # 单账号失败不影响后续账号
    assert calls == ["MP_WXS_1", "MP_WXS_2"]
    assert stats["errors"] == 1
    assert stats["notify"] == 1


def test_no_accounts_noop(db, patch_session, monkeypatch):
    monkeypatch.setattr(weread_client, "node_available", lambda: True)
    assert run_subscription_check(db) == {"accounts": 0, "notify": 0, "auto": 0, "errors": 0}


def test_expired_pending_cleanup(db, patch_session, monkeypatch, stub_delivery, enqueue,
                                 no_cookie_profile):
    """超过 7 天的 pending 登记在分发前被清理。"""
    _account(db, synckey=10)
    stale = WxPendingArticle(
        account_id="MP_WXS_1", doc_url="https://mp.weixin.qq.com/s/old",
        title="旧文", article_time=1,
        created_at=datetime.now(timezone.utc) - timedelta(days=8),
    )
    db.add(stale)
    db.commit()
    monkeypatch.setattr(weread_client, "call_sync", lambda *a, **k: {"articles": [], "synckey": 11})
    monkeypatch.setattr(weread_client, "node_available", lambda: True)

    run_subscription_check(db)

    assert db.query(WxPendingArticle).count() == 0
