"""微信公众号抓取补丁：用 Playwright 替换 camoufox。

wechat_service.py 的 _fetch_page_html 默认使用 AsyncCamoufox 反检测浏览器，
但 camoufox 在代理环境下因 SSL 下载失败无法安装（gh:daijro/camoufox releases）。
我们已有可工作的 Playwright Chromium，因此用 monkey-patch 替换该方法。

不修改 vendor/ArchiveBOT 代码，补丁在 fetcher.fetch_article 调用前生效。
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _patch_wechat_fetch_page_html(cls=None) -> None:
    """替换 WechatService._fetch_page_html 为 Playwright 同步实现。

    cls 可传 None（自动从 services.wechat_service 导入 WechatService）。
    camoufox 版本约 70 行异步代码，Playwright 版本同样功能约 30 行同步代码。
    保留全部特性：cookie 加载、CAPTCHA 检测、运行时内容读取、超时等待。
    """
    if cls is None:
        from services.wechat_service import WechatService as _WS

        cls = _WS
    # 仅在尚未打过补丁时执行
    if getattr(cls, "_patch_applied", False):
        return

    from playwright.sync_api import sync_playwright

    _GENERIC_TITLES = getattr(cls, "_GENERIC_TITLES", ())
    _COOKIES_PATH = getattr(cls, "_COOKIES_PATH", "")

    def _fetch_page_html(self, url: str, headless: bool = True) -> str:
        """用 Playwright Chromium 抓取微信公众号文章 HTML（替代 camoufox）。

        关键：必须用 context + 设置 user_agent，且 cookie 在页面加载前通过
        context.add_cookies() 注入。直接 browser.new_page() 不加 context 时
        cookie 注入时机不对，微信会跳转验证码页。
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
                ),
            )

            # 加载 cookie（如有）——必须在 page 创建前注入 context
            cookies_path = getattr(self, "_COOKIES_PATH", _COOKIES_PATH)
            if cookies_path and os.path.exists(cookies_path):
                try:
                    cookies_list = json.loads(Path(cookies_path).read_text(encoding="utf-8"))
                    valid = []
                    for c in (cookies_list if isinstance(cookies_list, list) else []):
                        if not isinstance(c, dict) or not c.get("name"):
                            continue
                        # 保持原始域名不变。hy_token/hy_user 等腾讯统一认证 cookie
                        # 在 .tencent.com 域下跨子域自动生效。
                        valid.append({
                            "name": c["name"],
                            "value": c.get("value", ""),
                            "domain": c.get("domain", ".mp.weixin.qq.com"),
                            "path": c.get("path", "/"),
                        })
                    if valid:
                        context.add_cookies(valid)
                        logger.info("loaded %d WeChat cookies", len(valid))
                except Exception as e:
                    logger.warning("failed to load WeChat cookies: %s", e)

            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")

            # 等待内容加载 + CAPTCHA 检测（最多 20 秒）
            for _ in range(40):
                import time as _time
                _time.sleep(0.5)
                # CAPTCHA 检测：微信验证码页会重定向到 /mp/wappoc_appmsgcaptcha
                current_url = page.url
                if "appmsgcaptcha" in current_url.lower() or "antispider" in current_url.lower():
                    import services.wechat_service as ws_mod
                    raise ws_mod.WechatServiceError(
                        "WeChat verification/CAPTCHA detected. Please retry after solving verification."
                    )
                # 检查标题是否就绪（非通用标题）
                title = page.title()
                if title not in _GENERIC_TITLES:
                    break

            content = page.content()
            # 暂存原始页面 HTML：runner 据此建立 本地图片名→远程URL 映射
            # （vendor 下载图片后 md 里只剩 images/xx 本地引用，原 URL 不落盘）
            try:
                self._last_page_html = content
            except Exception:  # noqa: BLE001 - 暂存失败不影响抓取
                pass
            browser.close()
            return content

    cls._fetch_page_html = _fetch_page_html
    cls._patch_applied = True
    logger.info("wechat _fetch_page_html patched -> playwright")
