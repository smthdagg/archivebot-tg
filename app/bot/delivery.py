"""文件交付与消息发送（worker 侧）。

worker 用独立的 Bot 实例（不轮询）向用户发送文件与完成消息。
"""

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup

from app.config import get_settings

logger = logging.getLogger(__name__)

_bot: Bot | None = None


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
