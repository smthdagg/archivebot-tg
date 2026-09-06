"""Bot handler 单元测试（M8 遗留补全）——start/archive/history/menu。

策略：直接调用 handler 函数 + stub 的 Bot/Message/CallbackQuery/FSMContext，
handler 模块的 SessionLocal 被 monkeypatch 到独立 SQLite（tmp_path），Telegram API
全部 stub（不触网），enqueue_task stub 为记录器，SSRF 校验 stub 以避免 DNS 解析。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.bot.handlers.archive as archive_mod
import app.bot.handlers.history as history_mod
import app.bot.handlers.menu as menu_mod
import app.bot.handlers.start as start_mod
from app.archive.detector import extract_first_url
from app.bot.i18n import t
from app.config import Settings
from app.database.enums import FileType, TaskStatus, UserRole, UserStatus
from app.database.models import Base, File
from app.database.services import create_user
from app.tasks import manager as task_manager


# ---------------------------------------------------------------------------
# aiogram 对象替身（全部关联到单个测试内的 session factory）
# ---------------------------------------------------------------------------
class _FakeUser:
    def __init__(self, uid, username=None, full_name=None, language_code="en"):
        self.id = uid
        self.username = username
        self.full_name = full_name or (username or "Test User")
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
        self.markup_edits = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        return self

    async def edit_text(self, text, reply_markup=None):
        self.edits.append({"text": text, "reply_markup": reply_markup})

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


class _FakeBot:
    def __init__(self):
        self.sent_docs = []

    async def send_document(self, chat_id, document, **kwargs):
        self.sent_docs.append((chat_id, document))
        return True


class _FakeCallback:
    def __init__(self, user, data, message=None, bot=None):
        self.from_user = user
        self.data = data
        self.message = message or _FakeMessage(user, "")
        self.bot = bot or _FakeBot()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})


class _FakeFSM:
    """记录 update_data / set_state / clear 的内存 FSMContext 替身。"""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data = {}
        self.state = None

    async def get_data(self):
        return dict(self.data)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'handlers.db'}")
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
    """把各 handler 模块的 SessionLocal 指向测试专用 session factory。"""
    for mod in (start_mod, archive_mod, history_mod, menu_mod):
        monkeypatch.setattr(mod, "SessionLocal", db_factory)
    return db_factory


@pytest.fixture()
def enqueue(monkeypatch):
    calls = []

    def fake_enqueue(task_id, **_):
        calls.append(task_id)

    monkeypatch.setattr(archive_mod, "enqueue_task", fake_enqueue)
    monkeypatch.setattr(history_mod, "enqueue_task", fake_enqueue)
    return calls


@pytest.fixture()
def safe_ssrf(monkeypatch):
    """避免 validate_url 触发 DNS：允许公网 URL、拒绝字面私有地址。"""

    def fake_validate(url):
        return "192.168." not in url and "127.0.0.1" not in url and url.startswith("https://")

    monkeypatch.setattr(archive_mod, "validate_url", fake_validate)
    return fake_validate


def _mkuser(db, telegram_id, status=UserStatus.ACTIVE, role=UserRole.USER, **kw):
    u = create_user(db, telegram_id=telegram_id, language="en", role=role, status=status, **kw)
    db.commit()
    return u


def _mktask(db, user, url="https://example.com/article", platform="web", status=TaskStatus.COMPLETED, title=None):
    t = task_manager.create_task(
        db, user_id=user.id, chat_id=user.telegram_id, url=url, platform=platform, output_types=["PDF"]
    )
    t.status = status.value
    t.title = title
    db.commit()
    return t


def _find_button(kb, cb):
    for row in kb.inline_keyboard:
        for b in row:
            if b.callback_data == cb:
                return b
    return None


def _answered_texts(obj):
    return {a["text"] for a in obj.answers}


# ---------------------------------------------------------------------------
# start：/start 批准流（ensure_user PENDING/ACTIVE/DISABLED）
# ---------------------------------------------------------------------------
def test_start_non_admin_creates_pending_application(db, db_factory, patch_session):
    msg = _FakeMessage(_FakeUser(1001, "newbie", language_code="en"))
    fsm = _FakeFSM()
    _run(start_mod.on_start(msg, fsm))

    from app.database.services import get_user_by_telegram_id

    u = get_user_by_telegram_id(db, 1001)
    assert u is not None
    assert u.status == UserStatus.PENDING
    assert u.role == UserRole.USER
    assert len(msg.answers) == 1
    # 新用户收到「待审批」文案（application_id=1，独立临时库首个申请）
    assert msg.answers[0]["text"] == t("en-US", "user.pending", application_id="1")
    assert msg.answers[0]["reply_markup"] is None
    from app.database.models import UserApplication

    assert db.query(UserApplication).filter_by(telegram_id=1001).count() == 1


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_start_admin_creates_superadmin_active(db, db_factory, patch_session, monkeypatch):
    ADMIN_ID = 9001
    # ensure_user / user_language 读取 common 模块的 get_settings
    import app.bot.common as common_mod

    monkeypatch.setattr(common_mod, "get_settings", lambda: Settings(admin_ids=[ADMIN_ID], default_language="auto"))

    msg = _FakeMessage(_FakeUser(ADMIN_ID, "boss", language_code="en"))
    _run(start_mod.on_start(msg, _FakeFSM()))

    from app.database.services import get_user_by_telegram_id

    u = get_user_by_telegram_id(db, ADMIN_ID)
    assert u is not None
    assert u.status == UserStatus.ACTIVE
    assert u.role == UserRole.SUPER_ADMIN
    assert len(msg.answers) == 1
    assert msg.answers[0]["text"] == t("en-US", "start.welcome")
    kb = msg.answers[0]["reply_markup"]
    assert kb is not None
    assert _find_button(kb, "menu:admin") is not None  # 管理员才有管理中心


def test_start_existing_active_welcome(db, db_factory, patch_session):
    _mkuser(db, 2002, role=UserRole.USER)
    msg = _FakeMessage(_FakeUser(2002, "alice", language_code="en"))
    _run(start_mod.on_start(msg, _FakeFSM()))

    assert len(msg.answers) == 1
    kb = msg.answers[0]["reply_markup"]
    assert kb is not None
    assert _find_button(kb, "menu:new") is not None
    # 非管理员看不到管理中心
    assert _find_button(kb, "menu:admin") is None


def test_start_disabled_user(db, db_factory, patch_session):
    _mkuser(db, 3003, status=UserStatus.DISABLED)
    msg = _FakeMessage(_FakeUser(3003, "blocked", language_code="en"))
    fsm = _FakeFSM()
    _run(start_mod.on_start(msg, fsm))

    assert len(msg.answers) == 1
    assert "disabled" in msg.answers[0]["text"].lower()
    assert msg.answers[0]["reply_markup"] is None


# ---------------------------------------------------------------------------
# archive：URL → 校验 → 格式选择 → 建任务 / 取消
# ---------------------------------------------------------------------------
def test_archive_pending_user_rejected(db, patch_session, safe_ssrf):
    _mkuser(db, 1111, status=UserStatus.PENDING)
    msg = _FakeMessage(_FakeUser(1111, "pending_user"), text="https://example.com/a")
    _run(archive_mod.on_url_message(msg, _FakeFSM()))
    assert "pending" in msg.answers[0]["text"].lower()


def test_archive_disabled_user_rejected(db, patch_session, safe_ssrf):
    _mkuser(db, 2222, status=UserStatus.DISABLED)
    msg = _FakeMessage(_FakeUser(2222, "dis_user"), text="https://example.com/a")
    _run(archive_mod.on_url_message(msg, _FakeFSM()))
    assert "disabled" in msg.answers[0]["text"].lower()


def test_archive_no_url_invalid(db, patch_session, safe_ssrf):
    _mkuser(db, 3333)
    msg = _FakeMessage(_FakeUser(3333, "no_url"), text="just some text without a link")
    fsm = _FakeFSM()
    _run(archive_mod.on_url_message(msg, fsm))
    assert len(msg.answers) == 1
    assert "invalid" in msg.answers[0]["text"].lower()
    assert fsm.state is None  # 未进入格式选择状态


def test_archive_ssrf_url_invalid(db, patch_session, safe_ssrf):
    _mkuser(db, 4444)
    msg = _FakeMessage(_FakeUser(4444, "intruder"), text="https://192.168.1.1/admin")
    fsm = _FakeFSM()
    _run(archive_mod.on_url_message(msg, fsm))
    assert "invalid" in msg.answers[0]["text"].lower()


def test_archive_valid_url_enters_fsm(db, patch_session, safe_ssrf):
    _mkuser(db, 5555)
    msg = _FakeMessage(_FakeUser(5555, "downloader"), text="https://example.com/article")
    fsm = _FakeFSM()
    _run(archive_mod.on_url_message(msg, fsm))

    # 解析 + 平台/格式选择合并为一条消息（省一次 Telegram API 往返）
    assert len(msg.answers) == 1
    assert "parsing" in msg.answers[0]["text"].lower()
    kb = msg.answers[0]["reply_markup"]
    assert kb is not None
    assert _find_button(kb, "fmt:pdf") is not None
    assert fsm.state is not None and fsm.state.state.endswith("awaiting_format")
    assert fsm.data["pending_url"].startswith("https://")


def test_archive_format_selected_creates_task_and_enqueues(db, db_factory, patch_session, enqueue, safe_ssrf):
    user = _mkuser(db, 6666)
    msg = _FakeMessage(_FakeUser(6666, "fmt_user"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(6666, "fmt_user"), data="fmt:pdf", message=msg)
    fsm = _FakeFSM({"pending_url": "https://example.com/a", "pending_platform": "web"})
    _run(archive_mod.on_format_selected(cb, fsm))

    assert fsm.data == {}  # 状态已清
    assert msg.markup_edits == [None]  # 格式菜单已收起
    tasks, _ = task_manager.list_user_tasks(db, user.id)
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.QUEUED
    assert tasks[0].output_types == ["PDF"]
    assert tasks[0].status_message_id is not None
    assert enqueue == [tasks[0].id]


def test_archive_format_selected_missing_state(db, db_factory, patch_session, enqueue, safe_ssrf):
    user = _mkuser(db, 7777)
    msg = _FakeMessage(_FakeUser(7777, "no_state"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(7777, "no_state"), data="fmt:pdf", message=msg)
    _run(archive_mod.on_format_selected(cb, _FakeFSM()))

    assert msg.answers and msg.answers[0]["text"] == t("en-US", "url.no_url_found")
    # 未建任务
    tasks = task_manager.list_user_tasks(db, user.id)[0]
    assert tasks == []


def test_archive_cancel_ownership_denied(db, db_factory, patch_session, enqueue, safe_ssrf):
    owner = _mkuser(db, 8888)
    _mkuser(db, 9999)
    task = _mktask(db, owner, status=TaskStatus.QUEUED)
    msg = _FakeMessage(_FakeUser(9999, "attacker"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(9999, "attacker"), data=f"cancel:{task.id}", message=msg)
    _run(archive_mod.on_cancel(cb))

    assert cb.answers[-1]["show_alert"] is True  # denied 提示
    db.refresh(task)
    assert task.status == TaskStatus.QUEUED  # 未被取消


def test_archive_cancel_success(db, db_factory, patch_session, enqueue, safe_ssrf):
    owner = _mkuser(db, 10010)
    task = _mktask(db, owner, status=TaskStatus.QUEUED)
    msg = _FakeMessage(_FakeUser(10010, "owner"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(10010, "owner"), data=f"cancel:{task.id}", message=msg)
    _run(archive_mod.on_cancel(cb))

    db.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    assert msg.edits and "cancel" in msg.edits[0]["text"].lower()


def test_archive_cancel_completed_noop(db, db_factory, patch_session, enqueue, safe_ssrf):
    owner = _mkuser(db, 10011)
    task = _mktask(db, owner, status=TaskStatus.COMPLETED)
    msg = _FakeMessage(_FakeUser(10011, "owner"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(10011, "owner"), data=f"cancel:{task.id}", message=msg)
    _run(archive_mod.on_cancel(cb))

    db.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert msg.edits == []  # 终态不可取消，仅 answer()


def test_archive_cancel_malformed_data(db, db_factory, patch_session, enqueue, safe_ssrf):
    cb = _FakeCallback(
        _FakeUser(10012, "owner"), data="cancel:not-an-int", message=_FakeMessage(_FakeUser(10012, "owner"), "")
    )
    _run(archive_mod.on_cancel(cb))
    assert len(cb.answers) == 1  # ValueError 分支仅 answer，不报错


# ---------------------------------------------------------------------------
# history：分页 / 详情 / 获取文件 / 重新抓取 / 删除（所有权校验）
# ---------------------------------------------------------------------------
def test_history_empty(db, patch_session):
    user = _mkuser(db, 20001)
    msg = _FakeMessage(_FakeUser(20001, "h1"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(20001, "h1"), data="menu:history", message=msg)
    _run(history_mod.history_list(cb))
    assert msg.edits and msg.edits[0]["text"] == t("en-US", "history.empty")


def test_history_list_paginated(db, db_factory, patch_session):
    user = _mkuser(db, 20002)
    for i in range(7):  # 7 > PAGE_SIZE 5
        _mktask(db, user, url=f"https://example.com/{i}", title=None)
    msg = _FakeMessage(_FakeUser(20002, "h2"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(20002, "h2"), data="menu:history", message=msg)
    _run(history_mod.history_list(cb))
    kb = msg.edits[0]["reply_markup"]
    assert _find_button(kb, "hpage:2") is not None  # 有下一页
    assert "hist:" in " ".join(b.callback_data for row in kb.inline_keyboard for b in row)


def test_history_page_bottom(db, db_factory, patch_session):
    user = _mkuser(db, 20003)
    for i in range(7):
        _mktask(db, user, url=f"https://example.com/{i}")
    msg = _FakeMessage(_FakeUser(20003, "h3"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(20003, "h3"), data="hpage:0", message=msg)  # 越界下边界→1
    _run(history_mod.history_page(cb))
    kb = msg.edits[0]["reply_markup"]
    assert _find_button(kb, "hpage:1") is None  # 第一页无"上一页"


def test_history_detail_ownership_denied(db, db_factory, patch_session):
    owner = _mkuser(db, 20004)
    _mkuser(db, 20005)
    task = _mktask(db, owner, status=TaskStatus.COMPLETED)
    msg = _FakeMessage(_FakeUser(20005, "atk"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(20005, "atk"), data=f"hist:{task.id}", message=msg)
    _run(history_mod.history_detail(cb))
    assert cb.answers[-1]["show_alert"] is True
    assert msg.edits == []  # 未泄露详情


def test_history_detail_renders(db, db_factory, patch_session):
    owner = _mkuser(db, 20006)
    task = _mktask(db, owner, status=TaskStatus.COMPLETED)
    msg = _FakeMessage(_FakeUser(20006, "owner"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(20006, "owner"), data=f"hist:{task.id}", message=msg)
    _run(history_mod.history_detail(cb))
    assert msg.edits
    kb = msg.edits[0]["reply_markup"]
    assert _find_button(kb, f"hf:{task.id}:pdf") is not None
    assert _find_button(kb, f"hra:{task.id}") is not None
    assert _find_button(kb, f"hdel:{task.id}") is not None


def test_get_file_sends_by_file_id(db, db_factory, patch_session):
    owner = _mkuser(db, 20007)
    task = _mktask(db, owner, status=TaskStatus.COMPLETED)
    s = db_factory()
    t = s.get(type(task), task.id)
    t.files.append(
        File(task_id=task.id, user_id=owner.id, type=FileType.PDF.value, filename="a.pdf", telegram_file_id="FILEID-1")
    )
    s.commit()
    s.close()

    bot = _FakeBot()
    msg = _FakeMessage(_FakeUser(20007, "get"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(20007, "get"), data=f"hf:{task.id}:pdf", message=msg, bot=bot)
    _run(history_mod.get_file(cb))
    assert bot.sent_docs == [(owner.telegram_id, "FILEID-1")]


def test_get_file_no_file(db, db_factory, patch_session):
    owner = _mkuser(db, 20008)
    task = _mktask(db, owner, status=TaskStatus.COMPLETED)  # 无 file 落库
    bot = _FakeBot()
    cb = _FakeCallback(
        _FakeUser(20008, "get"), data=f"hf:{task.id}:pdf", message=_FakeMessage(_FakeUser(20008, "get")), bot=bot
    )
    _run(history_mod.get_file(cb))
    assert cb.answers[-1]["show_alert"] is True
    assert bot.sent_docs == []  # 不访问原站、不发文件


def test_rearchive_creates_new_task_keeps_old(db, db_factory, patch_session, enqueue):
    owner = _mkuser(db, 20009)
    old = _mktask(db, owner, status=TaskStatus.FAILED)
    msg = _FakeMessage(_FakeUser(20009, "ra"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(20009, "ra"), data=f"hra:{old.id}", message=msg)
    _run(history_mod.rearchive(cb))

    tasks, _ = task_manager.list_user_tasks(db, owner.id, per_page=10)
    assert len(tasks) == 2  # 旧记录保留 + 新任务
    new_id = max(t.id for t in tasks)
    assert enqueue == [new_id]
    # 旧记录仍存在（未删除）
    db.refresh(old)
    assert old.status == TaskStatus.FAILED


def test_rearchive_ownership_denied(db, db_factory, patch_session, enqueue):
    owner = _mkuser(db, 20010)
    _mkuser(db, 20011)
    task = _mktask(db, owner)
    cb = _FakeCallback(_FakeUser(20011, "atk"), data=f"hra:{task.id}", message=_FakeMessage(_FakeUser(20011, "atk")))
    _run(history_mod.rearchive(cb))
    assert cb.answers[-1]["show_alert"] is True
    assert enqueue == []


def test_open_original(db, db_factory, patch_session):
    owner = _mkuser(db, 20012)
    task = _mktask(db, owner, url="https://example.com/original")
    msg = _FakeMessage(_FakeUser(20012, "open"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(20012, "open"), data=f"hurl:{task.id}", message=msg)
    _run(history_mod.open_original(cb))
    assert msg.answers and msg.answers[0]["text"] == "https://example.com/original"


def test_delete_record(db, db_factory, patch_session):
    owner = _mkuser(db, 20013)
    task = _mktask(db, owner)
    s = db_factory()
    tobj = s.get(type(task), task.id)
    f = File(task_id=task.id, user_id=owner.id, type=FileType.PDF.value, filename="a.pdf", telegram_file_id="FID")
    tobj.files.append(f)
    s.commit()
    s.close()

    msg = _FakeMessage(_FakeUser(20013, "del"), chat_id=owner.telegram_id)
    cb = _FakeCallback(_FakeUser(20013, "del"), data=f"hdel:{task.id}", message=msg)
    _run(history_mod.delete_record(cb))

    s = db_factory()
    assert s.query(type(task)).filter_by(id=task.id).first() is None
    assert s.query(type(f)).filter_by(id=f.id).first() is None
    s.close()
    assert msg.edits and msg.edits[0]["text"] == t("en-US", "history.empty")


def test_delete_record_ownership_denied(db, db_factory, patch_session):
    owner = _mkuser(db, 20014)
    _mkuser(db, 20015)
    task = _mktask(db, owner)
    cb = _FakeCallback(_FakeUser(20015, "atk"), data=f"hdel:{task.id}", message=_FakeMessage(_FakeUser(20015, "atk")))
    _run(history_mod.delete_record(cb))
    assert cb.answers[-1]["show_alert"] is True
    s = db_factory()
    assert s.get(type(task), task.id) is not None  # 未被删除
    s.close()


# ---------------------------------------------------------------------------
# menu：新建 / 搜索 / 统计 / 设置 / 帮助
# ---------------------------------------------------------------------------
def test_menu_new_download(db, patch_session):
    user = _mkuser(db, 30001)
    msg = _FakeMessage(_FakeUser(30001, "new"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(30001, "new"), data="menu:new", message=msg)
    _run(menu_mod.new_download(cb))
    assert msg.edits and msg.edits[0]["text"] == t("en-US", "url.no_url_found")


def test_menu_help(db, patch_session):
    user = _mkuser(db, 30002)
    msg = _FakeMessage(_FakeUser(30002, "help"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(30002, "help"), data="menu:help", message=msg)
    _run(menu_mod.help_menu(cb))
    assert msg.edits and msg.edits[0]["reply_markup"] is not None


def test_menu_search_prompt_sets_state(db, patch_session):
    user = _mkuser(db, 30003)
    msg = _FakeMessage(_FakeUser(30003, "search"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(30003, "search"), data="menu:search", message=msg)
    fsm = _FakeFSM()
    _run(menu_mod.search_prompt(cb, fsm))
    assert fsm.state is not None and fsm.state.state.endswith("query")


def test_search_execute_results(db, db_factory, patch_session):
    user = _mkuser(db, 30004)
    _mktask(db, user, url="https://example.com/alpha")
    _mktask(db, user, url="https://example.com/beta", platform="weibo")
    msg = _FakeMessage(_FakeUser(30004, "q"), text="alpha")
    _run(menu_mod.search_execute(msg, _FakeFSM()))
    assert msg.answers and "alpha" in " ".join(a["text"].lower() for a in msg.answers)


def test_search_execute_empty(db, db_factory, patch_session):
    user = _mkuser(db, 30005)
    _mktask(db, user, url="https://example.com/alpha")
    msg = _FakeMessage(_FakeUser(30005, "q"), text="nonexistent-zzz")
    _run(menu_mod.search_execute(msg, _FakeFSM()))
    assert msg.answers and msg.answers[0]["text"] == t("en-US", "search.empty")


def test_menu_stats(db, db_factory, patch_session):
    user = _mkuser(db, 30006)
    _mktask(db, user, status=TaskStatus.COMPLETED)
    _mktask(db, user, status=TaskStatus.COMPLETED)
    _mktask(db, user, status=TaskStatus.FAILED)
    msg = _FakeMessage(_FakeUser(30006, "stats"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(30006, "stats"), data="menu:stats", message=msg)
    _run(menu_mod.stats(cb))
    text = msg.edits[0]["text"]
    assert "3" in text and "2" in text  # 总任务 3 / 已完成 2


def test_menu_settings(db, patch_session):
    user = _mkuser(db, 30007)
    msg = _FakeMessage(_FakeUser(30007, "set"), chat_id=user.telegram_id)
    cb = _FakeCallback(_FakeUser(30007, "set"), data="menu:settings", message=msg)
    _run(menu_mod.settings(cb))
    assert msg.edits and "settings" in msg.edits[0]["text"].lower()


# ---------------------------------------------------------------------------
# extract_first_url 轻量冒烟（供 archive 用例自检）
# ---------------------------------------------------------------------------
def test_extract_first_url_basics():
    assert extract_first_url("see https://a.b/c") == "https://a.b/c"
    assert extract_first_url("no link here") is None


# ---------------------------------------------------------------------------
# 申请暗号（REGISTRATION_CODE）
# ---------------------------------------------------------------------------

def _set_code(db, code="letmein"):
    from app.database.models import SystemSetting
    db.add(SystemSetting(key="registration_code", value=code))
    db.commit()


def test_start_with_code_required_asks_code_no_application(db, db_factory, patch_session):
    _set_code(db)

    msg = _FakeMessage(_FakeUser(4001, "guest", language_code="en"))
    fsm = _FakeFSM()
    _run(start_mod.on_start(msg, fsm))

    # 提示输入暗号，未创建申请单
    assert "enter the registration code" in msg.answers[0]["text"]
    from app.database.models import UserApplication
    assert db.query(UserApplication).count() == 0
    assert fsm.data["reg_user_id"] > 0


def test_start_code_wrong_rejected_no_application(db, db_factory, patch_session):
    _set_code(db)

    msg = _FakeMessage(_FakeUser(4002, "guest", language_code="en"))
    fsm = _FakeFSM()
    _run(start_mod.on_start(msg, fsm))
    wrong = _FakeMessage(_FakeUser(4002, "guest"), text="wrong-code")
    _run(start_mod.on_code_entered(wrong, fsm))

    assert "Incorrect code" in wrong.answers[0]["text"]
    from app.database.models import UserApplication
    assert db.query(UserApplication).count() == 0


def test_start_code_correct_creates_application(db, db_factory, patch_session):
    _set_code(db)

    msg = _FakeMessage(_FakeUser(4003, "guest", language_code="en"))
    fsm = _FakeFSM()
    _run(start_mod.on_start(msg, fsm))
    good = _FakeMessage(_FakeUser(4003, "guest"), text=" letmein ")  # 允许首尾空白
    _run(start_mod.on_code_entered(good, fsm))

    from app.database.models import UserApplication
    apps = db.query(UserApplication).all()
    assert len(apps) == 1 and apps[0].status == "PENDING"
    assert "申请 ID：1" in good.answers[-1]["text"] or "Application ID" in good.answers[-1]["text"]


def test_start_pending_application_reused_not_duplicated(db, db_factory, patch_session):
    """未配置暗号（开放申请）时，重复 /start 复用同一申请单。"""
    msg = _FakeMessage(_FakeUser(4004, "again", language_code="en"))
    _run(start_mod.on_start(msg, _FakeFSM()))
    _run(start_mod.on_start(msg, _FakeFSM()))

    from app.database.models import UserApplication
    assert db.query(UserApplication).count() == 1
