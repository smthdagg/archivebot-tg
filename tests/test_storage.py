"""存储管理测试：任务目录、大小统计、清理。"""

import pytest

from app.storage.cleanup import CleanupService
from app.storage.manager import StorageManager


@pytest.fixture()
def storage(tmp_path):
    return StorageManager(root=tmp_path / "storage")


def test_new_task_dir(storage: StorageManager):
    d = storage.new_task_dir()
    assert d.exists()
    assert (d / "images").exists()
    assert storage.total_size() == 0


def test_task_size(storage: StorageManager):
    d = storage.new_task_dir()
    (d / "article.md").write_text("hello world" * 100)
    assert storage.task_size(d.name) == 1100
    assert storage.total_size() == 1100


def test_cleanup_deletes_oldest_and_protects_active(storage: StorageManager):
    d1 = storage.new_task_dir()
    (d1 / "a").write_bytes(b"x" * 100)
    d2 = storage.new_task_dir()
    (d2 / "b").write_bytes(b"y" * 100)
    service = CleanupService(storage)

    # target_bytes=0：触发清理直到低于目标；d2 受保护不应被删
    deleted = service.run_cleanup(protected=lambda: {d2.name}, target_bytes=0)
    assert deleted == [d1.name]
    assert not d1.exists()
    assert d2.exists()
