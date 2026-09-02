"""存档文件命名：标题_YYYY-MM-DD_HHMM.ext（规格 §10）。

仅存正文与图片，命名跟随标题，追加当天存档时间，保证排序与去重。
"""

import re
from datetime import datetime, timezone

# 文件名非法字符（Windows + POSIX + Telegram 显示友好）
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n]')
_WHITESPACE = re.compile(r"\s+")


def sanitize_title(title: str, max_len: int = 60) -> str:
    """标题转为文件名安全片段：去非法字符、压缩空白、截断。"""
    if not title:
        return "Untitled"
    # 去非法字符
    safe = _ILLEGAL.sub("_", title)
    # 压缩空白为单空格，转下划线（避免 Telegram/Markdown 解析）
    safe = _WHITESPACE.sub(" ", safe).strip()
    safe = safe.replace(" ", "_")
    # 压缩连续下划线
    safe = re.sub(r"_+", "_", safe).strip("_")
    if not safe:
        return "Untitled"
    # 按字符截断（不按字节，中文友好），保留首段
    if len(safe) > max_len:
        safe = safe[:max_len].rstrip("_")
    return safe


def archive_basename(title: str, archived_at: datetime | None = None) -> str:
    """返回 标题_YYYY-MM-DD_HHMM 的基础名（不含扩展名）。"""
    archived_at = archived_at or datetime.now(timezone.utc)
    # 转本地时区的“当天”语义：用 UTC 日期（与 deployed 的 pdf 页脚一致）
    date_str = archived_at.strftime("%Y-%m-%d_%H%M")
    return f"{sanitize_title(title)}_{date_str}"


def without_fragment(url: str) -> str:
    """去掉 URL 的 fragment (#page2) 供 page_id 去重与封面提取使用。"""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
