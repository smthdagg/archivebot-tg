"""文件交付与消息发送（worker 侧）。

worker 用独立的 Bot 实例（不轮询）向用户发送文件与完成消息。

关键设计：worker 是同步进程（rq），异步调用通过 run_async() 在一个
**持久事件循环**上执行。不能反复用 asyncio.run()——它每次创建并关闭
新循环，而 aiogram Bot 的 aiohttp session 绑定在首次使用的循环上，
循环关闭后 session 失效，后续调用抛 "Event loop is closed"。
"""

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup

from app.config import get_settings

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """返回持久事件循环（惰性创建；意外关闭时重建并重置 Bot）。"""
    global _loop, _bot
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _bot = None  # 旧 Bot 的 session 绑定在已关闭的循环上，必须重建
    return _loop


def run_async(coro):
    """在持久事件循环上执行协程（替代 asyncio.run，保持 session 存活）。"""
    return _get_loop().run_until_complete(coro)


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=get_settings().telegram_bot_token)
    return _bot


async def send_document(
    chat_id: int,
    file_path: Path,
    *,
    caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> str:
    """上传文件到 Telegram，返回 file_id（规格 §13）。"""
    bot = get_bot()
    document = FSInputFile(str(file_path))
    message = await bot.send_document(
        chat_id, document, caption=caption, reply_markup=reply_markup
    )
    file_id = message.document.file_id
    logger.info("uploaded %s -> %s (%s)", file_path.name, file_id, message.document.file_size)
    return file_id


async def send_video(
    chat_id: int,
    file_path: Path,
    *,
    caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> str:
    """上传视频文件到 Telegram，返回 file_id（规格 §13，Phase 2 视频交付）。"""
    bot = get_bot()
    video = FSInputFile(str(file_path))
    message = await bot.send_video(
        chat_id, video, caption=caption, reply_markup=reply_markup
    )
    file_id = message.video.file_id
    logger.info("uploaded video %s -> %s (%s)", file_path.name, file_id, message.video.file_size)
    return file_id


async def send_message(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> int:
    bot = get_bot()
    message = await bot.send_message(
        chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    return message.message_id


async def edit_message(chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    bot = get_bot()
    try:
        await bot.edit_message_text(
            chat_id, message_id, text, reply_markup=reply_markup
        )
    except Exception as e:  # noqa: BLE001 - 消息可能已被编辑/删除，尽力而为
        logger.debug("edit_message failed: %s", e)
