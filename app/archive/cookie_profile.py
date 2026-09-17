"""Cookie Profile：为登录类网站注入用户自备 cookie（Phase 2，设计规格 § 登录网站）。

规格红线：不绕过付费墙/访问控制；profile 仅用于**用户自己登录过的网站**。
系统只在任务明确指定 profile 名时才注入，绝不自动对任何网站附加 cookie。

ArchiveBOT 各平台服务的 cookie 消费方式不同，因此注入策略分三类：

- **文件型**（WECHAT / REDDIT）：服务运行时从类属性 `_COOKIES_PATH`
  指向的 Cookie-Editor 格式 JSON（list of {name, value, domain, path}）读取 cookie。
  注入 = 把 profile 的 cookie 写入临时文件，并在服务调用期间把该临时文件路径
  临时挂到 `cls._COOKIES_PATH`，调用结束恢复（复用 ssrf_guard 的包装/猴子补丁思路，
  不修改 vendor 源码）。
- **方法型**（ZHIHU / TWITTER / XHS）：服务通过 `_get_cookies()` 或同等
  auth 钩子读取 cookie。注入 = 临时替换对应读取方法返回 profile 的 cookie，
  调用结束恢复。
- **不支持**（WEB / WEIBO）：webpage_service 与 weibo_service 无 cookie
  读取，这些平台忽略 profile，仅记录在案（docs/05）。
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
    {Platform.WECHAT.value, Platform.REDDIT.value}
)
# 特殊网站：财新等 WEB 平台下的白名单域名，允许用 Playwright/cookie 注入抓取
SPECIAL_WEB_COOKIE_SITES: dict[str, list[str]] = {
    "caixin": [".caixin.com", "weekly.caixin.com"],
}
METHOD_BASED_PLATFORMS: frozenset[str] = frozenset(
    {Platform.ZHIHU.value, Platform.TWITTER.value, Platform.XHS.value}
)

# 明确不支持 cookie 注入的平台（记录在案，见 docs/05）
UNSUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {Platform.WEB.value, Platform.WEIBO.value}
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
        Platform.TWITTER.value: ".x.com",
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
    - 方法型平台：临时替换 `_get_cookies` 类方法，或在 Twitter/XHS 这种
      构造参数型平台上通过钩子注入。
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
    # Twitter（X）：方法型特殊分支——Playwright scraper 靠 auth_token/ct0
    # 两个显式 cookie 登录态，不是文件型
    if platform_value == Platform.TWITTER.value:
        with _twitter_auth_injection(service_cls, cookies) as applied:
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


def _patch_twitter_cookie_attrs(cookies: list[dict[str, Any]]) -> None:
    """monkey-patch vendor 的 X 抓取上下文与引用推文提取（红线 1：不改 vendor）。

    1) _setup_browser 整段替换为极简形态：vendor 的重 stealth 段（fake
       chrome/media 原型劫持/permissions 瞲骗 + 老 UA 池）被 X 降级渲染
       （空推文 / Something went wrong）；实测极简形态（现代 UA + cookie
       原属性 + 无 init script）稳定。
    2) _extract_tweet_data 包装：vendor 的 Tweet 模型没有引用推文字段，
       提取后从 DOM 补抓 [data-testid="quoteTweet"]（作者/文本），经
       模块级暂存 _last_tweet_extras 带出（wechat_patch._last_page_html
       同款模式），fetcher 组装进产物。
    """
    from services import playwright_scraper as _ps

    if getattr(_ps, "_twitter_cookie_attrs_patched", False):
        return

    profile_by_name = {c["name"]: c for c in cookies if c.get("name") in ("auth_token", "ct0")}

    async def patched_setup(self):
        """极简浏览器上下文（实测可用形态）替换 vendor 的重 stealth 段。

        vendor 的 add_init_script（fake chrome/media 原型劫持/permissions
        瞲骗）与 en-US/New_York 环境、老 UA 池会被 X 降级渲染（空推文 /
        Something went wrong）。实测极简形态（现代 UA + cookie 原属性 +
        无 init script）稳定返回推文内容。
        """
        self.playwright = await _ps.async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        inject = []
        for name in ("auth_token", "ct0"):
            if name not in profile_by_name:
                continue
            c = profile_by_name[name]
            inject.append({
                "name": name,
                "value": c.get("value", ""),
                "domain": c.get("domain", ".x.com"),
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", name == "auth_token")),
                "secure": bool(c.get("secure", True)),
                "sameSite": c.get("sameSite", "Lax"),
            })
        if inject and self.context:
            try:
                await self.context.add_cookies(inject)
                _ps.info("[patch] injected twitter cookies (original attrs)")
            except Exception as e:  # noqa: BLE001
                _ps.warning(f"[patch] twitter cookie inject failed: {e}")

    _ps.TwitterPlaywrightScraper._setup_browser = patched_setup

    # 引用推文补抓：包装 _extract_tweet_data，成功提取后从 DOM 拿 quoteTweet
    if not getattr(_ps, "_twitter_extract_patched", False):
        original_extract = _ps.TwitterPlaywrightScraper._extract_tweet_data

        async def patched_extract(self, page, tweet_id: str):
            # 长推文默认截断（「显示更多/Show more」按钮）：展开后再提取，
            # 否则归档只有预览半截（实测 919 字全文只拿到 ~700 字）
            try:
                for sel in (
                    'article button:has-text("显示更多")',
                    'article button:has-text("Show more")',
                    '[data-testid="tweetText"] ~ * button:has-text("Show more")',
                ):
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click(timeout=3000)
                        await page.wait_for_timeout(1200)
                        break
            except Exception:  # noqa: BLE001 - 展开失败按预览提取
                pass
            data = await original_extract(self, page, tweet_id)
            extras: dict[str, str] = {}
            try:
                quoted = await page.evaluate(
                    """() => {
                      const q = document.querySelector('[data-testid="quoteTweet"]');
                      if (!q) return null;
                      const author = (q.querySelector('a[href*="/status/"] span')
                        && q.querySelector('a[href*="/status/"] span').textContent) || '';
                      const textEl = q.querySelector('[data-testid="tweetText"]');
                      const text = textEl ? textEl.innerText : q.innerText || '';
                      return {author: author.trim(), text: text.trim()};
                    }"""
                )
                if quoted:
                    extras["quoted_author"] = quoted.get("author", "")
                    extras["quoted_text"] = quoted.get("text", "")
            except Exception:  # noqa: BLE001 - 引用抓取失败不影响正文
                pass
            # 每次提取都重置暂存（防上一篇的引用串到下一篇）
            _ps._last_tweet_extras = extras
            return data

        _ps.TwitterPlaywrightScraper._extract_tweet_data = patched_extract
        _ps._twitter_extract_patched = True
    _ps._twitter_cookie_attrs_patched = True


@contextmanager
def _twitter_auth_injection(service_cls: type, cookies: list[dict[str, Any]]) -> Any:
    """让 Twitter 平台的 Playwright scraper 获得登录态。

    Profile 以 Cookie-Editor 列表形式存储「auth_token / ct0」两条，
    这里提取这两条并临时挂到 `TwitterService._twitter_auth_pair`，
    供 fetcher 在实例化 service 时传给构造器（不改 vendor 源码）。
    """
    wanted: dict[str, str] = {}
    for c in cookies:
        name = c.get("name")
        if name in ("auth_token", "ct0"):
            wanted[name] = str(c.get("value", ""))
    prev = getattr(service_cls, "_twitter_auth_pair", None)
    payload: dict[str, str] | None = wanted if wanted else None
    service_cls._twitter_auth_pair = payload
    if payload:
        # vendor 注入的 cookie 属性与真实登录态不符（Cookie 同意墙），改透传原属性
        try:
            _patch_twitter_cookie_attrs(cookies)
        except Exception:  # noqa: BLE001 - 补丁失败按 vendor 原行为
            pass
    try:
        yield payload
    finally:
        if prev is None:
            try:
                delattr(service_cls, "_twitter_auth_pair")
            except AttributeError:
                pass
        else:
            service_cls._twitter_auth_pair = prev


@contextmanager
def _method_based_injection(service_cls: type, cookies: list[dict[str, Any]]) -> Any:
    """临时替换 `_get_cookies` 类方法，返回 profile cookie。"""
    original = getattr(service_cls, "_get_cookies", None)

    def _patched(*args, **kwargs) -> list[dict[str, Any]]:
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

def resolve_profile_for_task(platform: str, url: str) -> str | None:
    """按平台/URL 自动关联任务可用的 cookie profile（建任务时调用）。

    - WEB 平台：按 SPECIAL_SITES 的 domains 匹配 URL（如财新）
    - 登录类平台（twitter/zhihu/xhs/reddit/wechat）：取第一个配置了该平台
      cookie 的 profile
    文件优先于 settings（profile 文件会被运行时回写，settings 有 lru_cache
    旧值）；容器内相对路径按 /app 兜底。失败返回 None。
    """
    import json
    from pathlib import Path

    from app.archive.cookie_registry import SPECIAL_SITES
    from app.config import get_settings
    from app.database.enums import Platform

    profiles: dict = {}
    path_str = get_settings().cookie_profiles_file
    if path_str:
        path = Path(path_str)
        if not path.is_absolute():
            path = Path("/app") / path
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    profiles = loaded
            except Exception:
                profiles = {}
    if not profiles:
        profiles = get_settings().cookie_profiles or {}

    if platform == Platform.WEB.value:
        for key, cfg in SPECIAL_SITES.items():
            if cfg.get("platform") != Platform.WEB.value:
                continue
            for domain in cfg.get("domains", []):
                if domain.lstrip(".") in (url or "") and key in profiles:
                    return key
        return None
    if platform in ("twitter", "zhihu", "xhs", "reddit", "wechat"):
        for key, platforms_map in profiles.items():
            if isinstance(platforms_map, dict) and platforms_map.get(platform):
                return key
    return None
