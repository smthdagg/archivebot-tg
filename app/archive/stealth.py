"""反检测浏览器加载器：优先 Patchright（Playwright 反检测 fork），回退 Playwright。

背景：Playwright 默认 Chromium 带 navigator.webdriver / CDP 痕迹等自动化特征，
财新、微信公众号等强风控站点会识别并主动使登录 session 失效（顶号）。
Patchright 在协议层抹除这些特征，API 与 Playwright 完全兼容，import 即用。

VPS 安装：pip install patchright && patchright install chromium（见 Dockerfile）。
"""

import logging

logger = logging.getLogger(__name__)

# Patchright 文档：不要传暴露自动化特征的 flag（如
# --disable-blink-features=AutomationControlled），默认参数即最佳隐身。
# 容器内必须保留 --no-sandbox / --disable-dev-shm-usage。
STEALTH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def get_async_playwright():
    """返回 (async_playwright 工厂, 引擎名)，优先 Patchright。"""
    try:
        from patchright.async_api import async_playwright

        return async_playwright, "patchright"
    except ImportError:
        from playwright.async_api import async_playwright

        return async_playwright, "playwright"


def get_sync_playwright():
    """返回 (sync_playwright 工厂, 引擎名)，优先 Patchright。"""
    try:
        from patchright.sync_api import sync_playwright

        return sync_playwright, "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright

        return sync_playwright, "playwright"
