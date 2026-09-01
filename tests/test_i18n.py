"""i18n 加载器测试。"""

from app.bot.i18n import resolve_language, t


def test_resolve_language():
    assert resolve_language("zh-CN", "en") == "zh-CN"
    assert resolve_language("en-US", "zh") == "en-US"
    assert resolve_language("auto", "zh-Hans") == "zh-CN"
    assert resolve_language("auto", "en") == "en-US"
    assert resolve_language("auto", "fr") == "en-US"  # 不支持语言回退英文


def test_translation_zh():
    assert "欢迎使用" in t("zh-CN", "start.welcome")
    assert t("zh-CN", "url.parsing")


def test_translation_en():
    assert "Welcome" in t("en-US", "start.welcome")


def test_placeholder_format():
    text = t("zh-CN", "url.platform", platform="微信公众号")
    assert "微信公众号" in text
    text = t("en-US", "url.platform", platform="WeChat")
    assert "WeChat" in text


def test_unknown_key_fallback():
    # 中文缺键 → 回退英文；都缺 → 返回键名
    assert t("zh-CN", "nonexistent.key.xyz") == "nonexistent.key.xyz"
