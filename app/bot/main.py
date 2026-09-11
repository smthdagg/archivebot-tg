"""Telegram Bot 进程入口（docker-compose 运行：python -m app.bot.main）。

aiogram 3.x long polling。用户管理与归档流程见 handlers/。
启动时注册 Telegram 命令菜单（/ 弹出列表）：普通用户与管理员两套作用域。
"""

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.bot.handlers import admin, archive, cookies, history, menu, start
from app.config import get_settings
from app.database.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("bot")

# 双语文案太长会截断（命令描述上限 256，但按钮列表要短）——中英并排短句
_USER_COMMANDS = [
    BotCommand(command="start", description="主菜单 / Main menu"),
]
_ADMIN_COMMANDS = _USER_COMMANDS + [
    BotCommand(command="cookies", description="Cookie 状态 / Cookie status"),
    BotCommand(command="set_cookie", description="更新 Cookie / Update cookies"),
]


async def _register_command_menus(bot: Bot) -> None:
    """默认作用域给普通用户命令；管理员私聊单独覆盖为全量命令。"""
    settings = get_settings()
    try:
        await bot.set_my_commands(_USER_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as e:  # noqa: BLE001 - 菜单注册失败不阻断启动
        logger.warning("set_my_commands(default) failed: %s", e)
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                _ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("set_my_commands(admin %s) failed: %s", admin_id, e)


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    init_db()

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(
        start.router,
        archive.router,
        history.router,
        menu.router,
        admin.router,
        cookies.router,
    )

    bot = Bot(token=settings.telegram_bot_token)
    await _register_command_menus(bot)
    logger.info("bot started, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
