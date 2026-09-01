"""i18n 加载器：按用户语言取文案（设计规格 §49）。

用法：
    text = t(lang, "archive.completed", title=..., source=...)
lang 取值：zh-CN / en-US / auto（调用方决定最终语言）。
"""

import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED = ("zh-CN", "en-US")
FALLBACK = "en-US"


@lru_cache
def _load(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / lang / "messages.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_language(preferred: str, telegram_lang: str | None = None) -> str:
    """auto 时回退到 Telegram language_code，再回退默认。"""
    if preferred in SUPPORTED:
        return preferred
    if telegram_lang and telegram_lang.lower().startswith("zh"):
        return "zh-CN"
    if telegram_lang and telegram_lang.lower().startswith("en"):
        return "en-US"
    return FALLBACK


def t(lang: str, key: str, **kwargs) -> str:
    """取文案并做 {placeholder} 格式化；缺键时回退英文，再缺则返回键名。"""
    table = _load(lang if lang in SUPPORTED else FALLBACK)
    template = table.get(key)
    if template is None and lang != FALLBACK:
        template = _load(FALLBACK).get(key)
    if template is None:
        return key
    return template.format(**kwargs) if kwargs else template
