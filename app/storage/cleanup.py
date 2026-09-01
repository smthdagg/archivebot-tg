"""存储清理（设计规格 §31/§32）。

规则：
- 达到软限（800MB）时后台清理；达到硬限（1GB）时禁止新任务并立即清理。
- 优先删除：已上传 Telegram 的、最旧的、失败/取消任务的临时文件。
- 禁止删除：PROCESSING / UPLOADING 状态的文件（由调用方提供受保护集合）。
- 清理目标：释放到约 200MB。
"""

import logging
from collections.abc import Callable, Iterable

from app.storage.manager import StorageManager, _size_of, get_storage

logger = logging.getLogger(__name__)

# 目录归属/状态读取失败时视为可删（最保守之外的安全兜底）
_TASK_DIR_STATUS_FILE = "metadata.json"


class CleanupService:
    def __init__(self, storage: StorageManager | None = None) -> None:
        self.storage = storage or get_storage()

    def run_cleanup(
        self,
        protected: Callable[[], Iterable[str]] | None = None,
        *,
        target_bytes: int | None = None,
    ) -> list[str]:
        """清理到目标水位，返回被删除的任务目录名。

        protected：返回当前禁止删除的任务 uuid 集合（PROCESSING/UPLOADING）。
        target_bytes：覆盖配置的清理目标（测试用）。
        """
        target = target_bytes if target_bytes is not None else self.storage.cleanup_target
        total = self.storage.total_size()
        if total <= target:
            return []

        protected_set = set(protected()) if protected else set()
        # 最旧在前
        candidates = [
            d for d in self.storage.task_dirs() if d.name not in protected_set
        ]

        deleted: list[str] = []
        for task_dir in candidates:
            if self.storage.total_size() <= target:
                break
            name = task_dir.name
            logger.info("cleanup: deleting task dir %s (size=%s)", name, _size_of(task_dir))
            self.storage.delete_task(name)
            deleted.append(name)
        return deleted


# 供 worker 定期调用：软限触发后台清理
def cleanup_if_needed(protected: Callable[[], Iterable[str]] | None = None) -> list[str]:
    service = CleanupService()
    if not service.storage.over_soft():
        return []
    return service.run_cleanup(protected)
