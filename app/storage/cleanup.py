"""存储清理（设计规格 §31/§32）。

规则：
- 达到软限（800MB）时后台清理；达到硬限（1GB）时禁止新任务并立即清理。
- 优先删除：已上传 Telegram 的、最旧的、失败/取消任务的临时文件。
- 禁止删除：PROCESSING / UPLOADING 状态的文件（由调用方提供受保护集合）。
- 清理目标：释放到约 200MB。

cleanup_if_needed 是 worker 每任务完成后的接线入口：超过软限则
清理到 target，保护运行中任务与当前任务目录，并写 STORAGE_CLEANUP 审计。
"""

import logging
from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from app.database.enums import AuditAction
from app.database.services import audit
from app.storage.manager import StorageManager, _size_of, get_storage
from app.tasks.manager import processing_storage_uuids

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


# 供 worker 每任务完成后调用：软限触发后台清理（M5 遗留接线）
def cleanup_if_needed(
    storage: StorageManager | None = None,
    db: Session | None = None,
    *,
    current_uuid: str | None = None,
    target_bytes: int | None = None,
    soft_limit_bytes: int | None = None,
    protected: Callable[[], Iterable[str]] | None = None,
) -> list[str]:
    """超过软限则后台清理到目标，返回被删除的任务目录名。

    - storage：注入的 StorageManager（默认进程单例）。
    - db：提供时，保护所有运行中（PROCESSING/UPLOADING）任务的目录，
      并在实际删除后写 STORAGE_CLEANUP 审计、提交。
    - current_uuid：当前任务目录，一并保护（幂等：完成时已删则无影响）。
    - target_bytes / soft_limit_bytes：覆盖配置（测试用）。
    - protected：额外受保护目录集合（可选）。
    """
    service = CleanupService(storage)

    total = service.storage.total_size()
    soft = soft_limit_bytes if soft_limit_bytes is not None else service.storage.soft_limit
    if total <= soft:
        return []

    base: set[str] = set()
    if db is not None:
        base.update(processing_storage_uuids(db))
    if current_uuid:
        base.add(current_uuid)

    def _protected() -> Iterable[str]:
        extra = set(protected()) if protected else set()
        return base | extra

    deleted = service.run_cleanup(_protected, target_bytes=target_bytes)
    if deleted and db is not None:
        audit(db, action=AuditAction.STORAGE_CLEANUP, details={"deleted": deleted})
        db.commit()
        logger.info("soft-limit cleanup deleted %s task dirs: %s", len(deleted), deleted)
    return deleted
