"""数据库层测试：模型 CRUD、用户服务、任务所有权。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.enums import TaskStatus, UserRole, UserStatus
from app.database.models import Base, Task, User
from app.database.services import create_user, get_user_by_telegram_id
from app.tasks import manager as task_manager


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_create_and_get_user(db):
    user = create_user(db, telegram_id=1001, username="alice", language="zh-CN")
    db.commit()
    fetched = get_user_by_telegram_id(db, 1001)
    assert fetched is not None
    assert fetched.username == "alice"
    assert fetched.status == UserStatus.PENDING
    assert fetched.role == UserRole.USER


def test_task_ownership(db):
    u1 = create_user(db, telegram_id=1)
    u2 = create_user(db, telegram_id=2)
    db.commit()

    task = task_manager.create_task(
        db,
        user_id=u1.id,
        chat_id=111,
        url="https://example.com",
        platform="web",
        output_types=["PDF"],
    )
    db.commit()

    # 所有者可访问
    got = task_manager.get_task_for_user(db, task.id, u1.id)
    assert got.id == task.id

    # 其它用户越权 → AccessDeniedError
    with pytest.raises(task_manager.AccessDeniedError):
        task_manager.get_task_for_user(db, task.id, u2.id)


def test_task_status_transitions(db):
    u1 = create_user(db, telegram_id=3)
    db.commit()
    task = task_manager.create_task(
        db, user_id=u1.id, chat_id=1, url="https://e.com", platform="web", output_types=["PDF"]
    )
    db.commit()
    assert task.status == TaskStatus.QUEUED

    task_manager.request_cancel(db, task)
    assert task.status == TaskStatus.CANCELLED
