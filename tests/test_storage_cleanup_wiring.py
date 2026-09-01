"""M5 存储软限自动清理接线测试（cleanup_if_needed）。

覆盖三个验收点：
1. 超过软限触发清理；
2. 保护运行中（DB）任务目录与当前任务目录；
3. 清理到 target 即停止（不误删剩余）。
"""

import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.enums import AuditAction, TaskStatus
from app.database.models import AuditLog, Base, Task
from app.database.services import create_user
from app.storage.cleanup import cleanup_if_needed
from app.storage.manager import StorageManager


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _new_dir(storage: StorageManager, mtime: float):
    d = storage.new_task_dir()
    (d / "f").write_bytes(b"x" * 100)
    os.utime(d, (mtime, mtime))
    return d


def test_below_soft_limit_does_not_cleanup(tmp_path):
    storage = StorageManager(root=tmp_path / "s")
    _new_dir(storage, mtime=1000.0)

    deleted = cleanup_if_needed(
        storage=storage, soft_limit_bytes=500, target_bytes=0
    )
    assert deleted == []
    # 未触发，不删任何目录
    assert storage.total_size() == 100


def test_over_soft_limit_triggers_cleanup_to_target(tmp_path):
    storage = StorageManager(root=tmp_path / "s")
    _new_dir(storage, mtime=1000.0)
    _new_dir(storage, mtime=2000.0)
    assert storage.total_size() == 200

    deleted = cleanup_if_needed(
        storage=storage, soft_limit_bytes=150, target_bytes=100
    )
    # 超过软限 150 → 触发；target 100：删最旧后余 100 == target 即停
    assert len(deleted) == 1
    assert storage.total_size() == 100


def test_cleanup_stops_at_target(tmp_path):
    storage = StorageManager(root=tmp_path / "s")
    for i in range(4):
        _new_dir(storage, mtime=1000.0 + i)

    deleted = cleanup_if_needed(
        storage=storage, soft_limit_bytes=300, target_bytes=200
    )
    # 400 总量，软限 300 触发；删到 <=200：删 2 个（300）后剩 200 即停
    assert len(deleted) == 2
    assert storage.total_size() == 200


def test_protects_processing_and_current_dirs(tmp_path, db):
    storage = StorageManager(root=tmp_path / "s")
    d_old = _new_dir(storage, mtime=1000.0)       # 最旧，应被删
    d_processing = _new_dir(storage, mtime=2000.0)  # 运行中，受保护
    d_current = _new_dir(storage, mtime=3000.0)   # 当前任务，受保护
    user = create_user(db, telegram_id=7001, role="USER", status="ACTIVE")
    # 一个运行中任务指向 d_processing
    db.add(Task(
        user_id=user.id, chat_id=1, url="https://e.com", platform="web",
        status=TaskStatus.FETCHING.value, storage_uuid=d_processing.name,
    ))
    db.commit()
    assert storage.total_size() == 300

    deleted = cleanup_if_needed(
        storage=storage, db=db, current_uuid=d_current.name,
        soft_limit_bytes=200, target_bytes=0,
    )
    assert deleted == [d_old.name]
    assert not d_old.exists()
    assert d_processing.exists()
    assert d_current.exists()

    # 实际删除后写 STORAGE_CLEANUP 审计
    logs = db.scalars(
        select(AuditLog).where(AuditLog.action == AuditAction.STORAGE_CLEANUP.value)
    ).all()
    assert len(logs) == 1
    assert logs[0].details == {"deleted": [d_old.name]}
