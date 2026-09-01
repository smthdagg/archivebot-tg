"""SSRF 防护（设计规格 §50）：抓取任意 URL 前拒绝内网/环回/云 metadata 地址。"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 常见云厂商 metadata endpoint 主机名
_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "169.254.169.254",  # AWS/GCP/Azure metadata
    "169.254.170.2",  # AWS ECS container credentials
    "100.100.100.200",  # 阿里云 metadata
}


def is_safe_host(host: str) -> bool:
    """校验主机名是否可抓取（域名解析后检查私有/环回/链路本地地址）。"""
    if not host:
        return False
    host = host.strip().lower().rstrip(".")
    if host in _METADATA_HOSTS:
        return False
    if host == "localhost":
        return False

    # 字面 IP 直接判断
    try:
        ip = ipaddress.ip_address(host)
        return _is_public_ip(ip)
    except ValueError:
        pass  # 是主机名，继续解析

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _is_public_ip(ip):
            return False
    return True


def validate_url(url: str) -> bool:
    """URL 级校验：scheme 必须 http/https 且主机安全。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    return is_safe_host(parsed.hostname)


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:  # noqa: SLF001
    if _in_allowed_cidrs(ip):
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _in_allowed_cidrs(ip: ipaddress._BaseAddress) -> bool:
    """命中运营者豁免段（如代理 fake-IP 段）则视为可抓取。"""
    from app.config import get_settings

    raw = get_settings().ssrf_allowed_cidrs.strip()
    if not raw:
        return False
    for cidr in raw.split(","):
        try:
            if ip in ipaddress.ip_network(cidr.strip()):
                return True
        except ValueError:
            continue
    return False
