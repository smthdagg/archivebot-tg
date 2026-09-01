"""process_task 全链路集成测试（stub Telegram 交付，不触网）。

覆盖：状态机推进 → 上传落库 file_id → 完成消息 → 本地清理；
     超过 Telegram 50MB 的产物跳过而非任务失败；
     worker 侧 SSRF 复验拒绝内网 URL。
"""

from pathlib import Path

import pytest

from app.archive.types import ArchiveResult
from app.database.database import SessionLocal, init_db
from app.database.enums import TaskStatus
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
    u = User(telegram_id=991000 + _user_seq["n"], username="e2e", language="zh-CN", status="ACTIVE")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def stub_delivery(monkeypatch):
    calls: dict = {"docs": [], "messages": []}

    async def fake_send_document(chat_id, path, *, caption=None, reply_markup=None):
        calls["docs"].append((path.name, path.stat().st_size))
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


def _fake_run_archive_factory(sizes: dict[str, int]):
    """生成 run_archive 替身：在 task_dir 落真实文件，尺寸由 sizes 指定。"""

    def fake_run_archive(
        *, task_dir, url, platform, output_types, archive_time=None,
        cookie_profile=None, on_status=None,
    ):
        if on_status:
            on_status(TaskStatus.FETCHING)
            on_status(TaskStatus.GENERATING_PDF)

        def _write(name: str, size: int) -> Path:
            path = task_dir / name
            if name.endswith((".md", ".txt")):
                path.write_text("# 标题\n\n正文" + "x" * size, encoding="utf-8")
            else:
                with path.open("wb") as f:
                    f.write(b"%PDF-1.4 stub" if name.endswith(".pdf") else b"PK-stub")
                    f.truncate(max(size, 16))
            return path

        result = ArchiveResult(
            task_dir=task_dir,
            platform=platform.value,
            title="端到端标题",
            author="作者",
            source_url=url,
            excerpt="这是第一行摘要。\n这是第二行摘要。\n这是第三行摘要。",
        )
        if "md" in sizes:
            result.markdown_path = _write("article.md", sizes["md"])
        if "pdf" in sizes:
            result.pdf_path = _write("article.pdf", sizes["pdf"])
        if "zip" in sizes:
            result.images = [_write("images.zip", sizes["zip"])]
        return result

    return fake_run_archive


def _new_task(db, user, url="https://example.com/article") -> Task:
    return create_task(
        db,
        user_id=user.id,
        chat_id=user.telegram_id,
        url=url,
        platform="web",
        output_types=["PDF", "MARKDOWN"],
    )


def test_process_task_happy_path(db, user, stub_delivery, monkeypatch):
    monkeypatch.setattr(jobs, "run_archive", _fake_run_archive_factory({"md": 100, "pdf": 4096, "zip": 2048}))
    task = _new_task(db, user)

    outcome = jobs.process_task(task.id)

    assert outcome == {"status": TaskStatus.COMPLETED}
    db.expire_all()
    task = db.get(Task, task.id)
    assert task.status == TaskStatus.COMPLETED
    assert task.title == "端到端标题"
    assert task.excerpt.startswith("这是第一行摘要。")

    files = list(task.files)
    assert {f.type for f in files} == {"PDF", "MARKDOWN", "IMAGES_ZIP"}
    assert all(f.telegram_file_id.startswith("FILEID::") for f in files)

    # 本地文件已清理（历史依赖 telegram_file_id，规格 §13）
    from app.storage.manager import get_storage

    assert not get_storage().task_dir(task.storage_uuid).exists()
    assert all(f.deleted_at is not None for f in files)

    texts = "\n".join(stub_delivery["messages"])
    assert "端到端标题" in texts
    assert "归档完成" in texts


def test_process_task_oversized_pdf_skipped(db, user, stub_delivery, monkeypatch):
    """PDF 超过 50MB：跳过该文件并发提示，其余产物正常交付，任务不失败。"""
    over = 51 * 1024 * 1024
    monkeypatch.setattr(jobs, "run_archive", _fake_run_archive_factory({"md": 100, "pdf": over}))
    task = _new_task(db, user)

    outcome = jobs.process_task(task.id)

    assert outcome == {"status": TaskStatus.COMPLETED}
    db.expire_all()
    task = db.get(Task, task.id)
    assert task.status == TaskStatus.COMPLETED
    delivered = {f.type for f in task.files}
    assert delivered == {"MARKDOWN"}

    notes = [m for m in stub_delivery["messages"] if "50MB" in m]
    assert notes, "应发送超限提示"
    assert "article.pdf" in notes[0]


def test_process_task_ssrf_rejected_by_worker(db, user, stub_delivery):
    """worker 侧 SSRF 复验：内网 URL 直接 FAILED(INVALID_URL)，不进入抓取。"""
    task = _new_task(db, user, url="http://127.0.0.1:8080/admin")

    outcome = jobs.process_task(task.id)

    assert outcome == {"status": TaskStatus.FAILED}
    db.expire_all()
    task = db.get(Task, task.id)
    assert task.status == TaskStatus.FAILED
    assert task.error_code == "INVALID_URL"
    assert stub_delivery["docs"] == []
