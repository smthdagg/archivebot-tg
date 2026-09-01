"""SSRF 防护测试（规格 §50）。"""

from unittest.mock import patch

from app.archive import ssrf as ssrf_mod
from app.archive.ssrf import is_safe_host, validate_url


def test_private_and_loopback_blocked():
    assert not is_safe_host("127.0.0.1")
    assert not is_safe_host("localhost")
    assert not is_safe_host("10.0.0.5")
    assert not is_safe_host("192.168.1.1")
    assert not is_safe_host("172.16.0.1")
    assert not is_safe_host("169.254.169.254")
    assert not is_safe_host("metadata.google.internal")


def test_public_hosts_allowed():
    assert is_safe_host("8.8.8.8")
    # 主机名：mock DNS 解析为公网 IP
    with patch.object(ssrf_mod.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        assert is_safe_host("example.com")
        assert is_safe_host("mp.weixin.qq.com")


def test_hostname_resolving_to_private_blocked():
    with patch.object(ssrf_mod.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.9", 0))]):
        assert not is_safe_host("evil-internal.example")


def test_validate_url():
    with patch.object(ssrf_mod.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        assert validate_url("https://example.com/article")
    assert not validate_url("http://127.0.0.1/admin")
    assert not validate_url("ftp://example.com/file")
    assert not validate_url("http://169.254.169.254/latest/meta-data")
    assert not validate_url("not-a-url")
