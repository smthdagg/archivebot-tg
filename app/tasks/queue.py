"""Redis 队列封装（设计规格 §35）：rq 队列入队/出队。"""

import logging

import redis as redis_lib
from rq import Queue

from app.config import get_settings

logger = logging.getLogger(__name__)

_redis: redis_lib.Redis | None = None
_queue: Queue | None = None


def get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        # 不能开 decode_responses：rq 内部按 bytes 处理（intermediate_queue
        # 会对 lrange 结果调 .decode()），开了解码会直接崩
        _redis = redis_lib.Redis.from_url(get_settings().redis_url)
    return _redis


def get_queue(name: str = "archive") -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(name, connection=get_redis())
    return _queue


def enqueue_task(task_id: int, *, job_timeout: int = 1800) -> None:
    """把任务加入归档队列。任务以任务 ID 为 job id，保证去重。"""
    queue = get_queue()
    queue.enqueue(
        "app.tasks.jobs.process_task",
        task_id,
        job_id=f"task-{task_id}",
        job_timeout=job_timeout,
    )
    logger.info("enqueued task %s", task_id)


def enqueue_task_retry(task_id: int, attempt: int, *, job_timeout: int = 1800) -> None:
    """失败自动重试入队（M7）。

    重试 job 用派生 job id ``task-{id}-r{attempt}``，而不是复用原始
    ``task-{id}``：rq 的 enqueue 默认不查重，若在原始 job 仍在运行时用同一
    job_id 重入队，会覆盖共享的 ``rq:job:<id>`` Redis 键，导致队列里的重试
    job 数据被当前 job 的收尾逻辑覆写。派生唯一 id 后，去重由 Task 状态机
    幂等负担（``status != QUEUED`` 时 _process 直接跳过）。
    """
    queue = get_queue()
    queue.enqueue(
        "app.tasks.jobs.process_task",
        task_id,
        job_id=f"task-{task_id}-r{attempt}",
        job_timeout=job_timeout,
    )
    logger.info("retry enqueued task %s (attempt %s)", task_id, attempt)


def queue_stats() -> dict:
    queue = get_queue()
    return {
        "waiting": len(queue),
        "started": queue.started_job_registry.count,
        "failed": queue.failed_job_registry.count,
    }
