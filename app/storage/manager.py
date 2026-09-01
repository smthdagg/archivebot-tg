"""临时文件池管理（设计规格 §31/§33）。

目录结构：
    /storage/tasks/<task_uuid>/
        ├── metadata.json
        ├── article.md
        ├── article.pdf
        ├── cover.jpg
        └── images/001.jpg ...
"""

import logging
import uuid
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return 0


class StorageManager:
    """任务级临时目录与全局配额统计。"""

    def __init__(self, root: Path | None = None) -> None:
        settings = get_settings()
        self.root = root or settings.storage_dir
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tasks").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 任务目录
    # ------------------------------------------------------------------

    def new_task_dir(self) -> Path:
        """创建新任务目录，返回其路径。"""
        task_dir = self.root / "tasks" / uuid.uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=False)
        (task_dir / "images").mkdir(parents=True, exist_ok=True)
        return task_dir

    def task_dir(self, task_uuid: str) -> Path:
        return self.root / "tasks" / task_uuid

    def task_size(self, task_uuid: str) -> int:
        return _size_of(self.task_dir(task_uuid))

    def delete_task(self, task_uuid: str) -> None:
        path = self.task_dir(task_uuid)
        if path.exists():
            _rmtree(path)

    # ------------------------------------------------------------------
    # 全局配额
    # ------------------------------------------------------------------

    def total_size(self) -> int:
        tasks_root = self.root / "tasks"
        if not tasks_root.exists():
            return 0
        return sum(_size_of(p) for p in tasks_root.iterdir() if p.is_dir())

    @property
    def soft_limit(self) -> int:
        return get_settings().soft_limit_bytes

    @property
    def hard_limit(self) -> int:
        return get_settings().hard_limit_bytes

    @property
    def cleanup_target(self) -> int:
        return get_settings().cleanup_target_bytes

    def over_soft(self) -> bool:
        return self.total_size() > self.soft_limit

    def over_hard(self) -> bool:
        return self.total_size() > self.hard_limit

    def can_accept_new_task(self, estimated_bytes: int = 0) -> bool:
        """硬限保护：达到 1GB 时禁止新的大任务。"""
        return self.total_size() + estimated_bytes < self.hard_limit

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def task_dirs(self) -> list[Path]:
        tasks_root = self.root / "tasks"
        if not tasks_root.exists():
            return []
        return sorted(
            (p for p in tasks_root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# 进程级单例
_manager: StorageManager | None = None


def get_storage() -> StorageManager:
    global _manager
    if _manager is None:
        _manager = StorageManager()
    return _manager
