"""微信读书（weread-omni）桥接客户端。

公众号订阅跟踪经 weread-omni 的 SDK（Node 子进程）访问微信读书 API：搜索公众号
（``MP_WXS_<id>``）、订阅管理、增量拉文章列表。正文归档仍走本仓库现有直连管道，
本模块只负责「发现」。

协议（scripts/weread_bridge.mjs）：stdout 一行一 JSON；错误 exit 2，末行
``{"error": {"message", "code", "status"}}``。code 取上游 errCode：
``-2012``=登录超时（需重新扫码），``-2041``=需人工验证（不可自动完成）。

凭据：werad-omni 自管（WEREAD_CONFIG_DIR 重定向到 data/weread，容器内持久卷），
本模块不接触令牌值，日志不打敏感内容。
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

BRIDGE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "weread_bridge.mjs"

_LOGIN_TIMEOUT_SECONDS = 6 * 60  # 上游扫码 deadline 5 分钟 + 余量
_CALL_TIMEOUT_SECONDS = 60


class WereadError(Exception):
    """微信读书桥接失败基类（code 为上游 errCode，可为 None）。"""

    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class WereadTokenExpired(WereadError):
    """-2012：登录超时，需要管理员重新扫码（/weread_login）。"""


class WereadVerifyNeeded(WereadError):
    """-2041：需要人工验证，无界面客户端无法完成，只能告警转人工。"""


def _classify(payload: dict[str, Any]) -> WereadError:
    err = payload.get("error", {})
    code = err.get("code")
    message = err.get("message", "weread bridge error")
    if code == -2012:
        return WereadTokenExpired(message, code=code, status=err.get("status"))
    if code == -2041:
        return WereadVerifyNeeded(message, code=code, status=err.get("status"))
    return WereadError(message, code=code if isinstance(code, int) else None, status=err.get("status"))


def _bridge_env() -> dict[str, str]:
    env = os.environ.copy()
    cfg_dir = get_settings().weread_config_dir
    if cfg_dir:
        path = Path(cfg_dir)
        if not path.is_absolute():
            path = Path.cwd() / path
        env["WEREAD_CONFIG_DIR"] = str(path)
    return env


def _node_bin() -> str:
    node = shutil.which("node")
    if not node:
        raise WereadError("node runtime not found (weread-omni bridge requires Node >= 22.13)")
    return node


def node_available() -> bool:
    """Node 运行时是否可用（定时检查用它整轮快速跳过，不刷错误日志）。"""
    return shutil.which("node") is not None


def _parse_output(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise WereadError("weread bridge produced no output")
    return json.loads(lines[-1])


def _raise_if_error(payload: dict[str, Any]) -> dict[str, Any]:
    if "error" in payload:
        raise _classify(payload)
    return payload


def call_sync(op: str, *args: str, timeout: float = _CALL_TIMEOUT_SECONDS) -> dict[str, Any]:
    """同步调用（worker 侧线程用）。返回 bridge 的 JSON 结果。"""
    cmd = [_node_bin(), str(BRIDGE_PATH), op, *args]
    try:
        proc = subprocess.run(  # noqa: S603 - 参数来自代码内部调用点
            cmd, capture_output=True, text=True, timeout=timeout, env=_bridge_env(), check=False
        )
    except subprocess.TimeoutExpired as e:
        raise WereadError("weread bridge timeout") from e
    payload = _parse_output(proc.stdout)
    return _raise_if_error(payload)


async def call_async(op: str, *args: str, timeout: float = _CALL_TIMEOUT_SECONDS) -> dict[str, Any]:
    """异步调用（bot 进程内使用，避免阻塞事件循环）。"""
    cmd = [_node_bin(), str(BRIDGE_PATH), op, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_bridge_env()
        )
    except FileNotFoundError as e:
        raise WereadError("node runtime not found") from e
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise WereadError("weread bridge timeout") from e
    payload = _parse_output(stdout.decode("utf-8", errors="ignore"))
    return _raise_if_error(payload)


async def login_events_async(timeout: float = _LOGIN_TIMEOUT_SECONDS):
    """登录流程：逐行产出 bridge 事件（qr/status/done），异常时抛分类错误。

    用法：``async for ev in login_events_async(): ...``；事件形如
    ``{"event": "qr", "url": ...}`` / ``{"event": "done", ...}``。
    """
    cmd = [_node_bin(), str(BRIDGE_PATH), "login"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_bridge_env()
    )
    assert proc.stdout is not None
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("weread login non-json line: %s", text[:200])
                continue
            if "error" in event:
                raise _classify(event)
            yield event
            if event.get("event") == "done":
                return
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def status_sync() -> dict[str, Any]:
    """同步校验登录态（worker 侧用）。失败抛分类异常。"""
    return call_sync("status")
