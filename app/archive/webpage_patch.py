"""财新等特殊网站的 Playwright cookie 注入补丁（WEB 平台的付费墙）。

WebpageService 本身不支持 cookie，caixin 的 36 条 .caixin.com 登录态
需要像 wechat_patch 那样在 Playwright context 创建时注入。
不修改 vendor 代码，用 monkey-patch 在 fetcher 调用前生效。
"""

import json
import logging
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _load_caixin_cookies() -> list[dict]:
    """从 COOKIE_PROFILES 的 caixin.caixin 读取，或直接读文件。"""
    settings = get_settings()
    profiles = settings.cookie_profiles
    if "caixin" in profiles and "caixin" in profiles["caixin"]:
        return profiles["caixin"]["caixin"]
    # 直接读文件作为兜底（避免 lru_cache 旧值）
    path_str = settings.cookie_profiles_file
    if path_str:
        p = Path(path_str)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data.get("caixin"), dict):
                    inner = data["caixin"].get("caixin", [])
                    if isinstance(inner, list):
                        return inner
            except Exception:
                pass
    return []


def _patch_webpage_cookies(cls=None) -> None:
    """让 WebpageService 在浏览器上下文创建时注入 caixin cookie。"""
    if cls is None:
        from services.webpage_service import WebpageService as _WS  # type: ignore[import]

        cls = _WS
    if getattr(cls, "_cookie_patch_applied", False):
        return

    orig_async_fetch = cls._async_fetch_with_readability

    async def _patched(self, url: str) -> dict | None:
        # 仅对 caixin 域名注入，避免污染其他 WEB 页面
        cookies = []
        if "caixin.com" in url:
            cookies = _load_caixin_cookies()

        if not cookies:
            return await orig_async_fetch(self, url)

        # 重写版：带 cookie 注入的 Playwright 流程

        from playwright.async_api import async_playwright

        from vendor.ArchiveBOT.services.webpage_service import _READABILITY_JS  # noqa: PLC0415

        readability_src = _READABILITY_JS.read_text(encoding="utf-8")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            # 注入 caixin 登录态
            valid = []
            for c in cookies:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                valid.append({
                    "name": c["name"],
                    "value": str(c.get("value", "")),
                    "domain": c.get("domain", ".caixin.com"),
                    "path": c.get("path", "/"),
                })
            if valid:
                await context.add_cookies(valid)
                logger.info("caixin: injected %d cookies for %s", len(valid), url[:60])

            page = await context.new_page()
            try:
                await self._goto_with_fallbacks(page, url)
                await page.evaluate("""
                    async () => {
                        await new Promise(resolve => {
                            let y = 0;
                            const timer = setInterval(() => {
                                window.scrollBy(0, 400);
                                y += 400;
                                if (y >= document.body.scrollHeight) {
                                    clearInterval(timer);
                                    window.scrollTo(0, 0);
                                    resolve();
                                }
                            }, 100);
                        });
                    }
                """)
                await page.wait_for_timeout(1000)
                await page.add_script_tag(content=readability_src)
                article = await page.evaluate("""
                    () => {
                        var doc = document.cloneNode(true);
                        var reader = new Readability(doc);
                        return reader.parse();
                    }
                """)
            finally:
                await browser.close()
            return article

    cls._async_fetch_with_readability = _patched
    cls._cookie_patch_applied = True
    logger.info("webpage _async_fetch_with_readability patched for caixin cookie injection")
