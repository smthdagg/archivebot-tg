"""requests 层 SSRF 守卫测试（重定向每一跳校验）。"""

from unittest.mock import patch

import pytest
import requests

from app.archive import ssrf as ssrf_mod
from app.archive import ssrf_guard


def test_check_url_blocks_internal_and_non_http():
    for url in (
        "http://127.0.0.1:8080/x",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "file:///etc/passwd",
    ):
        with pytest.raises(ssrf_guard.BlockedHostError):
            ssrf_guard._check_url(url)


def test_check_url_allows_public_host():
    with patch.object(
        ssrf_mod.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]
    ):
        ssrf_guard._check_url("https://example.com/article")


def test_guard_blocks_send_before_connecting():
    """守卫安装后，Session.send 在真正建立连接前就拦截内网目标。"""
    ssrf_guard.ensure_installed()
    prepared = requests.Request(method="GET", url="http://127.0.0.1:9/secret").prepare()
    session = requests.Session()
    with pytest.raises(ssrf_guard.BlockedHostError):
        session.send(prepared)


def test_ensure_installed_is_idempotent():
    ssrf_guard.ensure_installed()
    once = requests.Session.send
    ssrf_guard.ensure_installed()
    # 幂等：不叠加包装层
    assert requests.Session.send is once
