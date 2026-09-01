"""失败自动重试（M7 遗留）单元/集成测试。

覆盖：可重试错误触发重入队、次数耗尽后 FAILED、不可重试错误直接 FAILED 不重试。
stub Telegram 交付 + stub run_archive 抛 FetchError，不触网、不依赖 Redis
（重入队通过 monkeypatch 拦截 enqueue_task_retry 断言 job id 与次数）。
"""

import pytest

from app.archive.fetcher import FetchError
from app.config import get_settings
from app.database.database import SessionLocal, init_db
from app.database.enums import ErrorCode, TaskStatus
from app.database.models import Task, User
from app.tasks import jobs
from app.tasks.manager import create_task


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


_user_seq = {"n": 0}


@pytest.fixture()
def user(db):
    _user_seq["n"] += 1
    u = User(telegram_id=991200 + _user_seq["n"], username="retry", language="zh-CN", status="ACTIVE")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def stub_delivery(monkeypatch):
    calls: dict = {"docs": [], "messages": []}

    async def fake_send_document(chat_id, path, *, caption=None, reply_markup=None):
        calls["docs"].append(path.name)
        return f"FILEID::{path.name}"

    async def fake_send_message(chat_id, text, reply_markup=None, parse_mode=None):
        calls["messages"].append(text)
        return 1000 + len(calls["messages"])

    async def fake_edit_message(chat_id, message_id, text, reply_markup=None):
        calls["messages"].append(f"EDIT::{text}")

    monkeypatch.setattr(jobs.delivery, "send_document", fake_send_document)
    monkeypatch.setattr(jobs.delivery, "send_message", fake_send_message)
    monkeypatch.setattr(jobs.delivery, "edit_message", fake_edit_message)
    return calls


@pytest.fixture()
def retries(monkeypatch):
    """拦截重入队：记录 (task_id, attempt)，避免真实 Redis。"""
    calls: list[tuple[int, int]] = []

    def fake_enqueue_retry(task_id: int, attempt: int, *, job_timeout: int = 1800) -> None:
        calls.append((task_id, attempt))

    monkeypatch.setattr(jobs, "enqueue_task_retry", fake_enqueue_retry)
    return calls


def _raisable(code: ErrorCode):
    def _raiser(*, task_dir, url, platform, output_types, archive_time=None, on_status=None):
        if on_status:
            on_status(TaskStatus.FETCHING)
        raise FetchError(f"boom {code.value}", code=code)

    return _raiser


def _new_task(db, user) -> Task:
    return create_task(
        db,
        user_id=user.id,
        chat_id=user.telegram_id,
        url="https://example.com/article",
        platform="web",
        output_types=["PDF", "MARKDOWN"],
    )


def _fresh(db, task_id: int) -> Task:
    db.expire_all()
    return db.get(Task, task_id)


def test_retryable_error_schedules_retry(db, user, stub_delivery, retries, monkeypatch):
    """可重试错误（TIMEOUT）：重入队、次数 +1、状态回 QUEUED、job 派生 id。"""
    monkeypatch.setattr(jobs, "run_archive", _raisable(ErrorCode.TIMEOUT))
    task = _new_task(db, user)

    outcome = jobs.process_task(task.id)

    assert outcome == {"status": "RETRY"}
    assert retries == [(task.id, 1)], "应带 attempt=1 重入队"
    task = _fresh(db, task.id)
    assert task.status == TaskStatus.QUEUED
    assert task.retry_count == 1
    assert task.error_code == "TIMEOUT"


def test_retry_exhausted_fails(db, user, stub_delivery, retries, monkeypatch):
    """已耗尽 retry_count：直接 FAILED，不再重入队。"""
    monkeypatch.setattr(jobs, "run_archive", _raisable(ErrorCode.TIMEOUT))
    task = _new_task(db, user)
    task.retry_count = get_settings().retry_count  # 已用到上限
    db.commit()

    outcome = jobs.process_task(task.id)

    assert outcome == {"status": TaskStatus.FAILED}
    assert retries == [], "耗尽后不应再重入队"
    task = _fresh(db, task.id)
    assert task.status == TaskStatus.FAILED
    assert task.retry_count == get_settings().retry_count
    assert task.error_code == "TIMEOUT"


def test_non_retryable_fails_without_retry(db, user, stub_delivery, retries, monkeypatch):
    """不可重试错误（SSRF INVALID_URL）：直接 FAILED，次数不变、不重入队。"""
    monkeypatch.setattr(jobs, "run_archive", _raisable(ErrorCode.INVALID_URL))
    task = _new_task(db, user)

    outcome = jobs.process_task(task.id)

    assert outcome == {"status": TaskStatus.FAILED}
    assert retries == []
    task = _fresh(db, task.id)
    assert task.status == TaskStatus.FAILED
    assert task.retry_count == 0
    assert task.error_code == "INVALID_URL"


def test_retry_loop_until_exhausted(db, user, stub_delivery, retries, monkeypatch):
    """全链路：持续 TIMEOUT，前两次重试、第三次（耗尽）FAILED。"""
    monkeypatch.setattr(jobs, "run_archive", _raisable(ErrorCode.TIMEOUT))
    task = _new_task(db, user)

    r1 = jobs.process_task(task.id)
    assert r1 == {"status": "RETRY"}
    assert _fresh(db, task.id).retry_count == 1

    r2 = jobs.process_task(task.id)
    assert r2 == {"status": "RETRY"}
    assert _fresh(db, task.id).retry_count == 2

    r3 = jobs.process_task(task.id)
    assert r3 == {"status": TaskStatus.FAILED}

    assert retries == [(task.id, 1), (task.id, 2)], "共两次重入队"
    final = _fresh(db, task.id)
    assert final.status == TaskStatus.FAILED
    assert final.retry_count == get_settings().retry_count
