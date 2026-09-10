"""weread_client 桥接客户端单元测试（mock 子进程，不触网）。

覆盖：JSON 解析与错误分类（-2012 → TokenExpired / -2041 → VerifyNeeded /
其他 → WereadError）、node 缺失检测、登录事件流解析。
"""

import subprocess
from typing import Any

import pytest

from app.archive import weread_client
from app.archive.weread_client import (
    WereadError,
    WereadTokenExpired,
    WereadVerifyNeeded,
)


def _fake_run(payload: dict[str, Any] | list[dict[str, Any]], returncode: int = 0):
    """构造 subprocess.run 替身：stdout 输出协议 JSON 行。"""
    lines = payload if isinstance(payload, list) else [payload]
    stdout = "\n".join(
        __import__("json").dumps(obj) for obj in lines
    ) + "\n"

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN002
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return fake_run


def test_call_sync_parses_result(monkeypatch):
    monkeypatch.setattr(weread_client.subprocess, "run", _fake_run({"ok": True, "account": "default"}))
    assert weread_client.call_sync("status") == {"ok": True, "account": "default"}


def test_token_expired_classification(monkeypatch):
    monkeypatch.setattr(
        weread_client.subprocess, "run",
        _fake_run({"error": {"message": "登录超时", "code": -2012}}, returncode=2),
    )
    with pytest.raises(WereadTokenExpired) as exc:
        weread_client.call_sync("articles", "MP_WXS_1")
    assert exc.value.code == -2012


def test_verify_needed_classification(monkeypatch):
    monkeypatch.setattr(
        weread_client.subprocess, "run",
        _fake_run({"error": {"message": "需要人工验证", "code": -2041}}, returncode=2),
    )
    with pytest.raises(WereadVerifyNeeded):
        weread_client.call_sync("search", "测试")


def test_generic_error_classification(monkeypatch):
    monkeypatch.setattr(
        weread_client.subprocess, "run",
        _fake_run({"error": {"message": "boom", "code": -2003}}, returncode=2),
    )
    with pytest.raises(WereadError) as exc:
        weread_client.call_sync("search", "x")
    assert exc.value.code == -2003
    assert not isinstance(exc.value, (WereadTokenExpired, WereadVerifyNeeded))


def test_no_output_raises(monkeypatch):
    monkeypatch.setattr(
        weread_client.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    with pytest.raises(WereadError):
        weread_client.call_sync("status")


def test_node_available(monkeypatch):
    monkeypatch.setattr(weread_client.shutil, "which", lambda _: None)
    assert weread_client.node_available() is False
    monkeypatch.setattr(weread_client.shutil, "which", lambda _: "/usr/bin/node")
    assert weread_client.node_available() is True


async def test_login_events_parses_stream(monkeypatch):
    """登录事件流：qr → done，done 后终止且杀掉子进程。"""
    lines = [
        b'{"event": "qr", "url": "https://qr"}\n',
        b'{"event": "done", "account": "default", "vid": 1}\n',
    ]

    class FakeProc:
        def __init__(self):
            self.stdout = self
            self.returncode = None
            self.killed = False

        async def readline(self):
            if lines:
                return lines.pop(0)
            return b""

        def kill(self):
            self.killed = True

        async def wait(self):
            return 0

    proc = FakeProc()

    async def fake_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(weread_client.asyncio, "create_subprocess_exec", fake_exec)
    events = []
    async for ev in weread_client.login_events_async():
        events.append(ev)
    assert [e["event"] for e in events] == ["qr", "done"]
    assert proc.killed is True


def test_bridge_env_injects_config_dir(monkeypatch, tmp_path):
    """WEREAD_CONFIG_DIR 注入：相对路径转绝对（基于 cwd）。"""
    monkeypatch.setattr(weread_client.os, "environ", {})
    monkeypatch.setattr(weread_client, "get_settings",
                        lambda: type("S", (), {"weread_config_dir": "data/weread"})())
    env = weread_client._bridge_env()
    assert env["WEREAD_CONFIG_DIR"].endswith("data/weread")
    import os as _os
    assert _os.path.isabs(env["WEREAD_CONFIG_DIR"])
