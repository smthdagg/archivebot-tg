"""Cookie Profile（Phase 2，登录类网站）测试。

覆盖：config 加载（env JSON / 配置文件 / 校验）；profile 解析与归一；
注入策略（wechat/xhs/reddit 文件型、zhihu 方法型、web/weibo 不支持）；
未知 profile 报错；任务流 create_task 落库 + process_task 透传。
"""

import json
from pathlib import Path

import pytest

from app.archive import cookie_profile
from app.archive.cookie_profile import (
    CookieProfileError,
    inject_cookies,
    load_profiles,
    resolve_cookies,
)
from app.config import Settings
from app.database.enums import Platform, TaskStatus
from app.tasks import jobs

# ---------------------------------------------------------------------------
# config 加载
# ---------------------------------------------------------------------------

def test_cookie_profiles_from_env_json(monkeypatch):
    monkeypatch.setenv(
        "COOKIE_PROFILES",
        json.dumps(
            {
                "wx_login": {
                    "wechat": [{"name": "sn", "value": "abc123", "domain": ".mp.weixin.qq.com"}]
                }
            }
        ),
    )
    s = Settings()
    assert s.cookie_profiles["wx_login"]["wechat"][0]["value"] == "abc123"


def test_cookie_profiles_from_file(monkeypatch, tmp_path):
    f = tmp_path / "profiles.json"
    f.write_text(
        json.dumps({"reddit_profile": {"reddit": [{"name": "session", "value": "v"}]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("COOKIE_PROFILES_FILE", str(f))
    s = Settings()
    assert "reddit_profile" in s.cookie_profiles


def test_cookie_profiles_missing_file_rejected(monkeypatch):
    monkeypatch.setenv("COOKIE_PROFILES_FILE", "/nonexistent/cookies.json")
    with pytest.raises(ValueError):
        Settings()


def test_cookie_profiles_invalid_structure_rejected(monkeypatch):
    monkeypatch.setenv(
        "COOKIE_PROFILES",
        json.dumps({"bad": [{"name": "x", "value": "y"}]}),  # 值与平台 dict 不符
    )
    with pytest.raises(ValueError):
        Settings()


def test_cookie_profiles_empty_default():
    # data/cookie_profiles.json 存在时会合并进 Settings（避免宿主机文件污染测试）
    s = Settings(_env_file=None, cookie_profiles_file="")
    assert s.cookie_profiles == {}


# ---------------------------------------------------------------------------
# profile 解析 / 归一
# ---------------------------------------------------------------------------

def test_load_profiles_from_settings(monkeypatch):
    monkeypatch.setenv(
        "COOKIE_PROFILES",
        json.dumps({"p": {"xhs": [{"name": "web_session", "value": "s"}]}}),
    )
    profiles = load_profiles(Settings())
    assert profiles["p"]["xhs"][0]["name"] == "web_session"


def test_resolve_cookies_no_profile_returns_none():
    assert resolve_cookies({}, None, Platform.WECHAT) is None


def test_resolve_unknown_profile_raises():
    with pytest.raises(CookieProfileError):
        resolve_cookies({"a": {"wechat": []}}, "missing", Platform.WECHAT)


def test_resolve_platform_without_cookies_returns_none():
    profiles = {"a": {"wechat": []}}
    assert resolve_cookies(profiles, "a", Platform.WEB) is None


def test_resolve_sanitizes_and_defaults_domain_path():
    profiles = {
        "a": {
            "wechat": [
                {"name": "sn", "value": "v"},  # 缺 domain/path → 默认
                {"name": "ct", "value": "x", "domain": ".mp.weixin.qq.com", "path": "/p"},
                {"name": None, "value": "z"},  # 缺 name → 丢弃
            ]
        }
    }
    cookies = resolve_cookies(profiles, "a", Platform.WECHAT)
    assert cookies is not None
    assert len(cookies) == 2
    assert cookies[0]["domain"] == ".mp.weixin.qq.com"
    assert cookies[0]["path"] == "/"
    assert cookies[1]["path"] == "/p"


# ---------------------------------------------------------------------------
# 注入策略
# ---------------------------------------------------------------------------

class FakeFileService:
    _COOKIES_PATH = "/original/cookies.json"


def test_file_based_injection_writes_and_restores(tmp_path):
    cookies = [{"name": "sn", "value": "v", "domain": ".mp.weixin.qq.com", "path": "/"}]
    written_path = None
    with inject_cookies(FakeFileService, Platform.WECHAT, cookies) as wp:
        written_path = wp
        # 调用期间：临时路径已挂载，原路径不再
        assert FakeFileService._COOKIES_PATH == written_path
        assert written_path != "/original/cookies.json"
        data = json.loads(Path(written_path).read_text(encoding="utf-8"))
        assert data == cookies
    # 调用结束后恢复原路径
    assert FakeFileService._COOKIES_PATH == "/original/cookies.json"
    # 临时文件已清理
    assert not Path(written_path).exists()


class FakeZhihuService:
    @classmethod
    def _get_cookies(cls):
        return [{"name": "orig", "value": "0"}]


def test_method_based_injection_patches_and_restores():
    with inject_cookies(FakeZhihuService, Platform.ZHIHU, [{"name": "z_c0", "value": "token"}]) as c:
        assert c == [{"name": "z_c0", "value": "token"}]
        patched = FakeZhihuService._get_cookies()
        assert patched[0]["name"] == "z_c0"
    restored = FakeZhihuService._get_cookies()
    assert restored[0]["name"] == "orig"


def test_inject_unsupported_platform_is_noop():
    class FakeWebService:
        pass

    with inject_cookies(FakeWebService, Platform.WEB, [{"name": "n", "value": "v"}]) as wp:
        assert wp is None
    assert not hasattr(FakeWebService, "_COOKIES_PATH")


def test_inject_no_cookies_is_noop():
    with inject_cookies(FakeFileService, Platform.WECHAT, None) as wp:
        assert wp is None
    assert FakeFileService._COOKIES_PATH == "/original/cookies.json"


# ---------------------------------------------------------------------------
# 平台支持清单
# ---------------------------------------------------------------------------

def test_support_matrix():
    assert cookie_profile.FILE_BASED_PLATFORMS == {"wechat", "reddit"}
    assert cookie_profile.METHOD_BASED_PLATFORMS == {"zhihu", "twitter", "xhs"}
    assert Platform.WEB.value in cookie_profile.UNSUPPORTED_PLATFORMS
    assert Platform.WEIBO.value in cookie_profile.UNSUPPORTED_PLATFORMS
    assert Platform.TWITTER.value not in cookie_profile.UNSUPPORTED_PLATFORMS


# ---------------------------------------------------------------------------
# 任务流：create_task 落库 + jobs 透传
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    from app.database.database import SessionLocal, init_db

    init_db()
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def user(db):
    from app.database.models import User

    u = User(telegram_id=990000, username="cp", language="zh-CN", status="ACTIVE")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def stub_delivery(monkeypatch):
    from app.bot import delivery

    async def fake_send_document(chat_id, path, *, caption=None, reply_markup=None):
        return f"FILEID::{path.name}"

    async def fake_send_message(chat_id, text, reply_markup=None, parse_mode=None):
        return 100

    async def fake_edit_message(chat_id, message_id, text, reply_markup=None):
        pass

    monkeypatch.setattr(delivery, "send_document", fake_send_document)
    monkeypatch.setattr(delivery, "send_message", fake_send_message)
    monkeypatch.setattr(delivery, "edit_message", fake_edit_message)


def test_create_task_persists_cookie_profile(db):
    from app.database.services import create_user
    from app.tasks.manager import create_task

    u = create_user(db, telegram_id=555000)
    db.commit()
    task = create_task(
        db,
        user_id=u.id,
        chat_id=u.telegram_id,
        url="https://mp.weixin.qq.com/s/abc",
        platform="wechat",
        output_types=["PDF"],
        cookie_profile="wx_login",
    )
    db.commit()
    assert task.cookie_profile == "wx_login"


def test_process_task_forwards_cookie_profile(db, user, stub_delivery, monkeypatch):
    """process_task 应把 task.cookie_profile 原样传入 run_archive。"""
    from app.archive.types import ArchiveResult
    from app.tasks.manager import create_task as ct

    seen = {}

    def fake_run_archive(
        *, task_dir, url, platform, output_types, archive_time=None,
        cookie_profile=None, on_status=None,
    ):
        seen["cookie_profile"] = cookie_profile
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "article.md").write_text("# t\n\n正文", encoding="utf-8")
        return ArchiveResult(
            task_dir=task_dir,
            platform=platform.value,
            title="标题",
            excerpt="a。\nb。\nc。",
            markdown_path=task_dir / "article.md",
        )

    monkeypatch.setattr(jobs, "run_archive", fake_run_archive)

    task = ct(
        db,
        user_id=user.id,
        chat_id=user.telegram_id,
        url="https://mp.weixin.qq.com/s/abc",
        platform="wechat",
        output_types=["MARKDOWN"],
        cookie_profile="wx_login",
    )
    db.commit()

    outcome = jobs.process_task(task.id)
    assert outcome == {"status": TaskStatus.COMPLETED}
    assert seen["cookie_profile"] == "wx_login"
