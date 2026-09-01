"""Web Admin 请求限流与登录锁定（规格 §50，M7 待办）。

进程内内存实现：单实例 /admin 场景足够，无外部依赖。时钟用可注入的 callable
（默认 ``time.monotonic``，避免系统时间回拨导致的误判），便于测试。

- :class:`SlidingWindowLimiter` —— 按 key（IP）的滑动窗口计数器，用于 /admin 全局限流。
- :class:`LoginThrottle` —— 登录失败连续 N 次/窗口 → 临时锁定时长（含指数退避），
  对已锁定 key 直接拒登。
- :class:`AdminRateLimitMiddleware` —— 挂在 ASGI 上，对 /admin 全局按 IP 限流，
  超限直接返回 429。

多副本部署需换共享后端（Redis），模块边界已按「store 独立、时钟可注入」隔离。
"""

import logging
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
DEFAULT_WINDOW = 60


def _default_clock() -> float:
    return time.monotonic()


class _BaseLock:
    """带单调时钟与线程安全基座。"""

    def __init__(self, clock=None):
        self._clock = clock or _default_clock
        self._lock = threading.Lock()


class SlidingWindowLimiter(_BaseLock):
    """滑动窗口：在 ``window`` 秒内每个 key 最多放行 ``limit`` 次。"""

    def __init__(self, limit: int, window: float, *, clock=None, _prune_threshold: int = 10_000):
        super().__init__(clock)
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0")
        self.limit = limit
        self.window = window
        self._prune_threshold = _prune_threshold
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """请求到达：窗口内未超限返回 True 并计数，否则返回 False。"""
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                if len(self._hits) >= self._prune_threshold:
                    self._prune_empty(now)
                return False
            q.append(now)
            return True

    def _prune_empty(self, now: float) -> None:
        """删除已全部过窗、无残留请求的空桶，防止 IP 风暴下内存无限增长。"""
        for k, q in list(self._hits.items()):
            while q and now - q[0] >= self.window:
                q.popleft()
            if not q:
                del self._hits[k]

    def remaining(self, key: str) -> int:
        """窗口内剩余配额（仅供展示/测试）。"""
        now = self._clock()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return self.limit
            while q and now - q[0] >= self.window:
                q.popleft()
            return max(0, self.limit - len(q))


class LoginThrottle(_BaseLock):
    """登录锁定：``window`` 秒内累计 ``max_failures`` 次失败 → 锁定 ``lockout`` 秒。

    连续多次触发锁定时，锁定基础时长按 ``backoff_base ** episode`` 指数退避
    （episode 仅在登录成功后清零），满足「连续 N 次失败临时锁定或指数退避」要求。
    """

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 900,
        lockout: float = 900,
        *,
        backoff_base: int = 2,
        clock=None,
        _prune_threshold: int = 10_000,
    ):
        super().__init__(clock)
        if max_failures < 1 or window <= 0 or lockout <= 0:
            raise ValueError("max_failures>=1, window>0, lockout>0 required")
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.backoff_base = backoff_base
        self._prune_threshold = _prune_threshold
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lockout_until: dict[str, float] = {}  # key -> 时钟值（该时刻后可再试）
        self._episode: dict[str, int] = {}  # key -> 连续锁定次数（登录成功清零）

    def is_blocked(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            until = self._lockout_until.get(key)
            if until is not None and now < until:
                return True
            return False

    def remaining_lockout(self, key: str) -> int:
        """剩余锁定秒数（0 表示未锁定）。"""
        now = self._clock()
        with self._lock:
            until = self._lockout_until.get(key, 0.0)
            if until <= now:
                return 0
            return int(until - now)

    def record_failure(self, key: str) -> bool:
        """记录一次失败；本次触发锁定时返回 True，否则 False。"""
        now = self._clock()
        with self._lock:
            q = self._failures[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            q.append(now)
            if len(q) < self.max_failures:
                return False
            # 触发锁定：按连续等级指数退避
            episode = self._episode.get(key, 0)
            duration = self.lockout * (self.backoff_base**episode)
            self._lockout_until[key] = now + duration
            self._episode[key] = episode + 1
            self._failures.pop(key, None)  # 锁定开始，清空窗口计数
            if len(self._lockout_until) >= self._prune_threshold:
                self._prune_expired(now)
            return True

    def record_success(self, key: str) -> None:
        """登录成功：清零该 key 全部锁定状态。"""
        with self._lock:
            self._failures.pop(key, None)
            self._lockout_until.pop(key, None)
            self._episode.pop(key, None)

    def _prune_expired(self, now: float) -> None:
        for k, until in list(self._lockout_until.items()):
            if now >= until:
                self._lockout_until.pop(k, None)
                self._failures.pop(k, None)


class AdminRateLimitMiddleware:
    """对 ``/admin`` 前缀请求按客户端 IP 滑动窗口限流，超限返回 429。

    非 /admin 路径（如 /healthz）直接透传，不计数。
    """

    def __init__(self, app, *, limiter=None):
        self.app = app
        self.limiter = limiter or SlidingWindowLimiter(DEFAULT_LIMIT, DEFAULT_WINDOW)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/admin"):
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            if not self.limiter.allow(ip):
                logger.warning("Web Admin rate limit exceeded: ip=%s", ip)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Too many requests"}',
                    }
                )
                return
        await self.app(scope, receive, send)


# 单例：/admin 登录与限流的进程内状态，参数取自配置。测试可在隔离实例上直接验证。
def _build_from_settings():
    from app.config import get_settings

    s = get_settings()
    limiter = SlidingWindowLimiter(s.web_admin_rate_limit, s.web_admin_rate_window_seconds)
    throttle = LoginThrottle(
        max_failures=s.web_admin_login_max_failures,
        window=s.web_admin_login_window_seconds,
        lockout=s.web_admin_login_lockout_seconds,
    )
    return limiter, throttle


request_limiter, login_throttle = _build_from_settings()
