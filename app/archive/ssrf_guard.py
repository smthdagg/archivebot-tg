"""requests 层 SSRF 防护（设计规格 §50 的纵深防御）。

入口处 validate_url 只校验一次用户提交的 URL，存在两类绕过：
1. 重定向：公网 URL 302 跳转到内网地址；
2. worker 侧遗漏校验的调用路径。

ArchiveBOT services 统一使用 requests（Session 实例或模块级 requests.get）。
requests 的每一跳（含重定向中间请求）都经过 Session.send，因此在该点安装
全局守卫即可覆盖所有跳数。注意两点残留风险：
- curl_cffi（知乎服务）与 Playwright 渲染不走 requests，不受此守卫约束；
- 校验在 DNS 解析前按 URL 主机名判定，无法防御解析结果在 connect 阶段
  被替换的 DNS rebinding（需 egress 网络策略兜底，见 docs/02-architecture.md）。
"""

import logging
from urllib.parse import urlparse

import requests

from app.archive import ssrf

logger = logging.getLogger(__name__)

_installed = False


class BlockedHostError(RuntimeError):
    """请求目标（含重定向中间跳）命中 SSRF 防护。"""


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedHostError(f"blocked non-http scheme: {url!r}")
    host = parsed.hostname or ""
    if not ssrf.is_safe_host(host):
        raise BlockedHostError(f"blocked internal/private host: {host!r}")


def ensure_installed() -> None:
    """安装全局 requests 守卫（幂等）。应在 worker 进程启动时尽早调用。"""
    global _installed
    if _installed:
        return
    _original_send = requests.Session.send

    def _guarded_send(session, prepared_request, **kwargs):
        try:
            _check_url(prepared_request.url)
        except BlockedHostError:
            logger.warning("SSRF guard blocked request to %s", prepared_request.url)
            raise
        return _original_send(session, prepared_request, **kwargs)

    requests.Session.send = _guarded_send
    _installed = True
    logger.info("requests SSRF guard installed")
