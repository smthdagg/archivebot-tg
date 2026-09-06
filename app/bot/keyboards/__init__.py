"""内联键盘构造（callback_data 仅作服务端校验入口 token，不携带敏感数据）。"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.i18n import t
from app.database.enums import UserRole


def main_menu(lang: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t(lang, "menu.new_download"), callback_data="menu:new")],
        [
            InlineKeyboardButton(text=t(lang, "menu.history"), callback_data="menu:history"),
            InlineKeyboardButton(text=t(lang, "menu.search"), callback_data="menu:search"),
        ],
        [
            InlineKeyboardButton(text=t(lang, "menu.stats"), callback_data="menu:stats"),
            InlineKeyboardButton(text=t(lang, "menu.settings"), callback_data="menu:settings"),
        ],
        [InlineKeyboardButton(text=t(lang, "menu.help"), callback_data="menu:help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text=t(lang, "menu.admin"), callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_selector(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "format.pdf"), callback_data="fmt:pdf"),
                InlineKeyboardButton(text=t(lang, "format.markdown"), callback_data="fmt:md"),
            ],
            [
                InlineKeyboardButton(text=t(lang, "format.images"), callback_data="fmt:img"),
                InlineKeyboardButton(text=t(lang, "format.all"), callback_data="fmt:all"),
            ],
            [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu")],
        ]
    )


def cancel_button(lang: str, task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "action.cancel"), callback_data=f"cancel:{task_id}")]
        ]
    )


def admin_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "admin.users"), callback_data="adm:users")],
            [InlineKeyboardButton(text=t(lang, "admin.pending"), callback_data="adm:pending")],
            [InlineKeyboardButton(text=t(lang, "admin.tasks"), callback_data="adm:tasks")],
            [InlineKeyboardButton(text=t(lang, "admin.status"), callback_data="adm:status")],
            [InlineKeyboardButton(text=t(lang, "admin.logs"), callback_data="adm:logs")],
            [InlineKeyboardButton(text=t(lang, "admin.regcode"), callback_data="adm:regcode")],
            [InlineKeyboardButton(text=t(lang, "action.back"), callback_data="menu")],
        ]
    )


def is_admin_role(role: str) -> bool:
    return role in (UserRole.ADMIN, UserRole.SUPER_ADMIN)
