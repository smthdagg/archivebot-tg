"""Cookie Profile：为登录类网站注入用户自备 cookie（Phase 2，设计规格 § 登录网站）。

规格红线：不绕过付费墙/访问控制；profile 仅用于**用户自己登录过的网站**。
系统只在任务明确指定 profile 名时才注入，绝不自动对任何网站附加 cookie。

ArchiveBOT 各平台服务的 cookie 消费方式不同，因此注入策略分三类：

- **文件型**（WECHAT / XHS / REDDIT）：服务运行时从类属性 `_COOKIES_PATH`
  指向的 Cookie-Editor 格式 JSON（list of {name, value, domain, path}）读取 cookie。
  注入 = 把 profile 的 cookie 写入临时文件，并在服务调用期间把该临时文件路径
  临时挂到 `cls._COOKIES_PATH`，调用结束恢复（复用 ssrf_guard 的包装/猴子补丁思路，
  不修改 vendor 源码）。
- **方法型**（ZHIHU）：服务通过 `_get_cookies()` 读取 cookie（DATA_DIR 下的
  `zhihu_cookies.json` 或 config z_c0）。注入 = 临时替换 `_get_cookies` 返回
  profile 的 cookie，调用结束恢复。
- **不支持**（WEB / WEIBO / TWITTER）：webpage_service 与 weibo_service 无 cookie
  读取；twitter 虽构造器接受 xreach_auth_token/ct0 但 dispatch 引用的 `save_tweet`
  方法不存在（抓取链路本身未接通）。这些平台忽略 profile，仅记录在案（docs/05）。
"""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.database.enums import Platform

logger = logging.getLogger(__name__)

# Cookie-Editor 兼容：每条 cookie 至少需要 name/value/domain/path。
# 文件型平台直接使用 Cookie-Editor 列表；zhihu 转成 Playwright 需要的同构 dict。
_REQUIRED_COOKIE_KEYS = ("name", "value", "domain", "path")

# 支持注入的平台 → 注入策略
FILE_BASED_PLATFORMS: frozenset[str] = frozenset(
    {Platform.WECHAT.value, Platform.XHS.value, Platform.REDDIT.value}
)
# 特殊网站：财新等 WEB 平台下的白名单域名，允许用 Playwright/cookie 注入抓取
SPECIAL_WEB_COOKIE_SITES: dict[str, list[str]] = {
    "caixin": [".caixin.com", "weekly.caixin.com"],
}
METHOD_BASED_PLATFORMS: frozenset[str] = frozenset({Platform.ZHIHU.value})

# 明确不支持 cookie 注入的平台（记录在案，见 docs/05）
UNSUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {
        Platform.WEB.value,
        Platform.WEIBO.value,
        Platform.TWITTER.value,
    }
)

# 已知会读取 cookie 的平台全集（用于提示）。视频类、其余平台一律不支持。
EXTERNAL_COOKIE_PLATFORMS: frozenset[str] = frozenset(
    set(FILE_BASED_PLATFORMS) | set(METHOD_BASED_PLATFORMS)
)


class CookieProfileError(ValueError):
    """profile 不存在或配置非法。"""


def load_profiles(settings: Any | None = None) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """从 settings 读取 Cookie Profiles（env JSON 与/或配置文件的并集）。

    settings 可显式传入以便测试；缺省用进程级缓存配置。
    """
    settings = settings or get_settings()
    return dict(getattr(settings, "cookie_profiles", None) or {})


def resolve_cookies(
    profiles: dict[str, dict[str, list[dict[str, Any]]]],
    profile_name: str | None,
    platform: Platform,
) -> list[dict[str, Any]] | None:
    """取指定 profile 中某平台对应的 cookie 列表。

    返回 None 表示无注入意图（未指定 profile，或该平台在 profile 中无 cookie）。
    """
    if not profile_name:
        return None
    profile = profiles.get(profile_name)
    if profile is None:
        raise CookieProfileError(f"unknown cookie profile: {profile_name!r}")
    cookies = profile.get(platform.value)
    if not cookies:
        return None
    return _sanitize_cookies(platform, cookies)


def _sanitize_cookies(
    platform: Platform, cookies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """过滤出 platform 能消费的 cookie 字段，丢弃缺 name/value 的项。"""
    cleaned: list[dict[str, Any]] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            logger.warning("drop cookie missing name/value in profile for %s", platform.value)
            continue
        entry = {
            "name": str(name),
            "value": str(value),
            "domain": c.get("domain") or _default_domain(platform),
            "path": c.get("path") or "/",
        }
        cleaned.append(entry)
    return cleaned


def _default_domain(platform: Platform) -> str:
    return {
        Platform.WECHAT.value: ".mp.weixin.qq.com",
        Platform.XHS.value: ".xiaohongshu.com",
        Platform.REDDIT.value: ".reddit.com",
        Platform.ZHIHU.value: ".zhihu.com",
    }.get(platform.value, ".example.com")


@contextmanager
def inject_cookies(
    service_cls: type,
    platform: Platform,
    cookies: list[dict[str, Any]] | None,
    *,
    special_site: str | None = None,
) -> Any:
    """在调用 ArchiveBOT 服务期间注入 profile cookie，结束后恢复原状。

    - 文件型平台：把 cookie 写入临时文件并临时接管 `cls._COOKIES_PATH`。
    - 方法型平台：临时替换 `_get_cookies` 类方法。
    - 特殊网站（如 caixin 的 WEB）：即使 platform=WEB 也允许按 url 域名匹配注入。
    - cookies 为空或平台不支持：直接放行（no-op）。
    """
    if not cookies:
        yield None
        return

    platform_value = platform.value
    # 特殊网站的 WEB 注入：财新等白名单域，客户端已验证可阅读即允许注入
    if special_site is not None and platform_value == Platform.WEB.value:
        with _file_based_injection(service_cls, cookies) as applied:
            # 让 webpage_service 也能通过 _COOKIES_PATH 消费（Playwright 读取）
            # 实际抓取走 trafilatura+Playwright，cookie 由 page.context.add_cookies 注入
            yield applied
        return
    if platform_value in FILE_BASED_PLATFORMS:
        with _file_based_injection(service_cls, cookies) as applied:
            yield applied
    elif platform_value in METHOD_BASED_PLATFORMS:
        with _method_based_injection(service_cls, cookies) as applied:
            yield applied
    else:
        # 平台不支持 cookie：不注入，仅记录（fetcher 会据此打日志）
        yield None


@contextmanager
def _file_based_injection(service_cls: type, cookies: list[dict[str, Any]]) -> Any:
    """写入临时 Cookie-Editor 文件并临时接管 `cls._COOKIES_PATH`。"""
    fd, tmp_path = tempfile.mkstemp(suffix=".cookies.json")
    try:
        with Path(tmp_path).open("w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
        original = getattr(service_cls, "_COOKIES_PATH", None)
        service_cls._COOKIES_PATH = tmp_path
        try:
            yield tmp_path
        finally:
            service_cls._COOKIES_PATH = original
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


@contextmanager
def _method_based_injection(service_cls: type, cookies: list[dict[str, Any]]) -> Any:
    """临时替换 `_get_cookies` 类方法，返回 profile cookie。"""
    original = getattr(service_cls, "_get_cookies", None)

    def _patched() -> list[dict[str, Any]]:
        return list(cookies)

    service_cls._get_cookies = _patched
    try:
        yield cookies
    finally:
        if original is not None:
            service_cls._get_cookies = original
        else:
            try:
                delattr(service_cls, "_get_cookies")
            except AttributeError:
                pass
