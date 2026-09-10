"""rq worker 进程入口（docker-compose 运行：python -m app.tasks.worker）。

除 rq 主循环外，还在 daemon 线程里跑公众号订阅增量检查（微信读书路线，
Phase 2）。纯 rq 无 scheduler（见 weread_check.py），线程循环即定时器；
部署假设单 worker 副本。
"""

import logging
import threading

from app.database.database import init_db
from app.tasks.queue import get_queue, get_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("worker")


def _weread_subscription_loop() -> None:
    from app.tasks.weread_check import subscription_loop

    subscription_loop()


def main() -> None:
    # 任何 requests 出网前先装 SSRF 守卫（含重定向每一跳，规格 §50）
    from app.archive import ssrf_guard

    ssrf_guard.ensure_installed()
    init_db()
    logger.info("worker starting, queue=%s", get_queue().name)
    # 每日 09:00 扫描 Cookie 过期并提醒（到期前 7 天/已过期）
    try:
        from app.archive.cookie_expiry import check_and_notify as _cookie_check

        expired = _cookie_check()
        if expired:
            import asyncio as _asyncio

            from app.archive.cookie_expiry import notify_expired_sites as _notify

            try:
                _asyncio.run(_notify(expired))
            except Exception as e:  # noqa: BLE001
                logger.warning("cookie expiry notify failed: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.debug("cookie expiry check skipped: %s", e)
    # 公众号订阅增量检查：daemon 线程（无 rq-scheduler，线程循环即定时器）
    try:
        threading.Thread(target=_weread_subscription_loop, name="weread-sub", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.warning("weread subscription thread failed to start: %s", e)
    from rq.worker import Worker

    worker = Worker([get_queue()], connection=get_redis())
    worker.work()


if __name__ == "__main__":
    main()
