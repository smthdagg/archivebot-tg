"""Web Admin 请求限流与登录锁定测试（规格 §50，M7）。"""

import pytest

from app.admin.ratelimit import AdminRateLimitMiddleware, LoginThrottle, SlidingWindowLimiter


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ---------------------------------------------------------------------------
# SlidingWindowLimiter
# ---------------------------------------------------------------------------

def test_limiter_blocks_after_limit_isolation_per_key():
    clk = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window=60, clock=clk)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    # 不同 key 独立计数
    assert limiter.allow("b") is True


def test_limiter_window_expiry_resets():
    clk = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window=60, clock=clk)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    clk.advance(61)
    assert limiter.allow("a") is True


def test_limiter_remaining_quota():
    clk = FakeClock()
    limiter = SlidingWindowLimiter(limit=3, window=60, clock=clk)
    assert limiter.remaining("x") == 3
    limiter.allow("x")
    assert limiter.remaining("x") == 2
    limiter.allow("x")
    limiter.allow("x")
    assert limiter.remaining("x") == 0


def test_limiter_validation():
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=0, window=60)
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=2, window=0)


# ---------------------------------------------------------------------------
# LoginThrottle
# ---------------------------------------------------------------------------

def test_login_lockout_after_n_failures():
    clk = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=900, lockout=900, clock=clk)
    assert throttle.record_failure("k") is False
    assert throttle.record_failure("k") is False
    # 第 3 次失败触发锁定
    assert throttle.record_failure("k") is True
    assert throttle.is_blocked("k") is True
    assert throttle.remaining_lockout("k") > 0


def test_login_lockout_expires():
    clk = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=900, lockout=900, clock=clk)
    throttle.record_failure("k")
    throttle.record_failure("k")
    throttle.record_failure("k")  # 锁定
    assert throttle.is_blocked("k") is True
    clk.advance(901)
    assert throttle.is_blocked("k") is False


def test_login_success_resets_failures():
    clk = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=900, lockout=900, clock=clk)
    throttle.record_failure("k")
    throttle.record_failure("k")
    throttle.record_success("k")
    # 成功清零后，重新计数而非直接累计触发
    throttle.record_failure("k")
    assert throttle.is_blocked("k") is False
    assert throttle.remaining_lockout("k") == 0


def test_login_exponential_backoff_escalates():
    clk = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=900, lockout=600, clock=clk)
    throttle.record_failure("k")
    throttle.record_failure("k")
    throttle.record_failure("k")  # 第一次锁定：600s
    assert throttle.remaining_lockout("k") == 600
    clk.advance(601)  # 锁定解除
    assert throttle.is_blocked("k") is False
    throttle.record_failure("k")
    throttle.record_failure("k")
    throttle.record_failure("k")  # 第二次锁定：600 * 2 = 1200s（指数退避）
    assert throttle.remaining_lockout("k") == 1200


def test_login_blocked_attempts_stay_blocked_until_expiry():
    clk = FakeClock()
    throttle = LoginThrottle(max_failures=2, window=900, lockout=300, clock=clk)
    throttle.record_failure("k")
    throttle.record_failure("k")  # 锁定
    assert throttle.is_blocked("k") is True
    clk.advance(299)
    assert throttle.is_blocked("k") is True
    clk.advance(2)
    assert throttle.is_blocked("k") is False


# ---------------------------------------------------------------------------
# AdminRateLimitMiddleware（无 httpx，直接驱动 ASGI）
# ---------------------------------------------------------------------------

async def _asgi_call(app, *, path: str, client=("192.0.2.1", 1234)):
    """发送最小 ASGI HTTP 请求，返回响应状态码与消息列表。"""
    received = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        received.append(msg)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": client,
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    for msg in received:
        if msg["type"] == "http.response.start":
            return msg["status"], received
    return None, received


async def test_middleware_429_at_limit():
    calls = []

    async def inner(scope, receive, send):
        calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw = AdminRateLimitMiddleware(inner, limiter=SlidingWindowLimiter(limit=3, window=60))
    for _ in range(3):
        status, _ = await _asgi_call(mw, path="/admin/")
        assert status == 200
    status, _ = await _asgi_call(mw, path="/admin/users")
    assert status == 429
    assert len(calls) == 3  # 超限请求未到达内层 app


async def test_middleware_non_admin_passthrough():
    calls = []

    async def inner(scope, receive, send):
        calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw = AdminRateLimitMiddleware(inner, limiter=SlidingWindowLimiter(limit=1, window=60))
    for _ in range(3):  # 非 /admin 路径不受限流
        status, _ = await _asgi_call(mw, path="/healthz")
        assert status == 200
    assert len(calls) == 3


async def test_middleware_per_ip_isolation():
    calls = []

    async def inner(scope, receive, send):
        calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw = AdminRateLimitMiddleware(inner, limiter=SlidingWindowLimiter(limit=2, window=60))
    status, _ = await _asgi_call(mw, path="/admin/", client=("192.0.2.1", 1))
    assert status == 200
    status, _ = await _asgi_call(mw, path="/admin/", client=("192.0.2.1", 1))
    assert status == 200
    # 同一 IP 已被限，换 IP 不受影响
    status, _ = await _asgi_call(mw, path="/admin/", client=("192.0.2.2", 2))
    assert status == 200
    assert len(calls) == 3
