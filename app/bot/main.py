"""Telegram Bot 进程入口（docker-compose 运行：python -m app.bot.main）。

aiogram 3.x long polling。用户管理与归档流程见 handlers/。
"""

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import admin, archive, cookies, history, menu, start, subscribe
from app.config import get_settings
from app.database.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("bot")


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    init_db()

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(
        start.router,
        # subscribe 必须在 archive 之前：archive 有任意含 http 文本的兜底
        # handler，/subscribe <文章URL> 会被它抢走
        subscribe.router,
        archive.router,
        history.router,
        menu.router,
        admin.router,
        cookies.router,
    )

    bot = Bot(token=settings.telegram_bot_token)
    logger.info("bot started, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
