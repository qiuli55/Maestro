"""网络弹性：retry / circuit breaker / rate limiter。

设计目标：
- 指数退避 + jitter：避免雪崩
- 区分 transient / permanent error：401/400 不重试，429/5xx/网络错重试
- 熔断：连续失败打熔断，快速失败（避免浪费 token）
- QPS 限流：避免触发上游限流（DeepSeek 免费档 10 QPS）

本模块 stateless + thread-safe：
- CircuitBreaker 多线程间共享（lock 保护）
- RateLimiter 用每线程令牌桶（lock-free）
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


# ====== transient error 识别 ======

def is_transient_error(exc: BaseException) -> bool:
    """判断异常是否值得重试。

    transient（重试）：429 限流、5xx 服务器错、网络错（超时、连接失败）、任何 IOError
    permanent（不重试）：400 参数错、401/403 鉴权错、404 资源错、用户输入错误
    """
    exc_name = type(exc).__name__
    exc_module = type(exc).__module__ or ""

    # openai SDK 异常类
    if exc_module.startswith("openai"):
        transient_names = {
            "RateLimitError",           # 429
            "APITimeoutError",          # 请求超时
            "APIConnectionError",       # 网络错
            "InternalServerError",      # 5xx
        }
        if exc_name in transient_names:
            return True
        # APIStatusError 要看 status code
        if exc_name == "APIStatusError":
            status = getattr(exc, "status_code", None)
            if status and 500 <= status < 600:
                return True
            return False
        # 其余 openai 异常（AuthenticationError, BadRequestError, NotFoundError）→ permanent
        return False

    # 通用网络/IO 错
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    # 兜底：未知异常默认 transient（保守重试，宁可多试不可漏判——
    # 万一是真的网络错被归类为未知异常，重试至少有机会成功）。
    # max_retries 上限保护实际不会无限重试。
    return True


# ====== RetryPolicy ======

@dataclass
class RetryPolicy:
    """退避策略：指数退避 + 抖动 + 上限。

    delay(attempt) = min(max_delay, base * 2^attempt) + random(0, jitter)
    """
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 1.0

    def delay(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** attempt)) + random.uniform(0, self.jitter)


# ====== CircuitBreaker ======

class CircuitBreaker:
    """熔断器：三态 CLOSED -> OPEN -> HALF_OPEN -> CLOSED。

    CLOSED：正常调用，连续 fail_threshold 次失败转 OPEN
    OPEN：直接拒绝（快速失败），reset_timeout 秒后转 HALF_OPEN
    HALF_OPEN：放行 1 次探测；成功转 CLOSED，失败回 OPEN
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, fail_threshold: int = 5, reset_timeout: float = 60.0):
        self.fail_threshold = fail_threshold
        self.reset_timeout = reset_timeout
        self._state = self.CLOSED
        self._fail_count = 0
        self._opened_at = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        """自动从 OPEN 切到 HALF_OPEN（懒检查）。"""
        if self._state == self.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                with self._lock:
                    if self._state == self.OPEN:
                        self._state = self.HALF_OPEN
        return self._state

    @property
    def fail_count(self) -> int:
        """只读：当前失败计数（测试/监控用）。"""
        return self._fail_count

    def allow(self) -> bool:
        """是否允许调用。OPEN 时返回 False（除非到了 HALF_OPEN 探测窗口）。"""
        s = self.state
        if s == self.CLOSED or s == self.HALF_OPEN:
            return True
        return False

    def record_success(self) -> None:
        with self._lock:
            self._fail_count = 0
            self._state = self.CLOSED
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._fail_count += 1
            if self._state == self.HALF_OPEN or self._fail_count >= self.fail_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        """手动重置（测试/调试）。"""
        with self._lock:
            self._state = self.CLOSED
            self._fail_count = 0
            self._opened_at = None


# ====== RateLimiter ======

class RateLimiter:
    """全局限流令牌桶（进程级，跨线程共享）。

    之前每线程独立一桶：36 个线程峰值 = 36 × QPS，限流形同虚设。
    现在用共享锁 + 共享桶：所有线程从同一个桶取令牌，QPS 是真正全局的。
    """
    def __init__(self, qps: float = 10.0, burst: int = 20):
        self.qps = qps
        self.burst = float(burst)
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._last_refill = time.monotonic()

    def _refill(self, now: float) -> None:
        self._tokens = min(self.burst, self._tokens + (now - self._last_refill) * self.qps)
        self._last_refill = now

    def acquire(self, timeout=None) -> bool:
        """阻塞直到拿到。timeout=None 永久等，否则最多等 N 秒。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            now = time.monotonic()
            with self._lock:
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self.qps
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.1))

    def reset(self) -> None:
        """手动重置（清掉所有线程的本地状态，测试用）。"""
        self._local = threading.local()


# ====== with_resilience decorator ======

class CircuitOpenError(RuntimeError):
    """熔断器打开时抛出（避免再调上游）。"""


def with_resilience(
    retry=None,
    breaker=None,
    limiter=None,
):
    """综合装饰器：限流 -> 熔断 -> 重试。

    使用：
        @with_resilience()
        def call_remote(): ...        # 默认策略

        @with_resilience(
            retry=RetryPolicy(max_retries=5),
            breaker=CircuitBreaker(fail_threshold=10),
        )
        def critical(): ...

    行为：
    1. RateLimiter.acquire() 先阻塞限流
    2. CircuitBreaker.allow() 检查熔断；OPEN 直接抛 CircuitOpenError
    3. 执行 fn，捕获异常：
       - transient + 有 retries：sleep delay + 重试
       - transient + 无 retries：重抛
       - permanent：不重试，立刻重抛
    4. 成功 -> breaker.record_success()
       失败 -> breaker.record_failure()
    """
    if retry is None:
        retry = RetryPolicy()
    if breaker is None:
        breaker = CircuitBreaker()
    if limiter is None:
        limiter = RateLimiter()

    def decorator(fn):
        def wrapper(*args, **kwargs):
            limiter.acquire()
            if not breaker.allow():
                raise CircuitOpenError(
                    f"circuit breaker open (fail_count>={breaker.fail_threshold})"
                )

            last_exc = None
            for attempt in range(retry.max_retries + 1):
                try:
                    result = fn(*args, **kwargs)
                    breaker.record_success()
                    return result
                except Exception as e:  # noqa: BLE001 — KeyboardInterrupt/SystemExit 不参与重试与熔断计数
                    last_exc = e
                    breaker.record_failure()
                    if not is_transient_error(e):
                        raise
                    if attempt >= retry.max_retries:
                        raise
                    time.sleep(retry.delay(attempt))
            raise last_exc  # pragma: no cover

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


# ====== 单例（项目内共享 breaker + limiter） ======

_shared_breaker = CircuitBreaker(fail_threshold=5, reset_timeout=60.0)
_shared_limiter = RateLimiter(qps=10.0, burst=20)


def shared_breaker() -> CircuitBreaker:
    return _shared_breaker


def shared_limiter() -> RateLimiter:
    return _shared_limiter


def reset_shared() -> None:
    """测试用：重置全局熔断器 + 限流器。"""
    global _shared_breaker, _shared_limiter
    _shared_breaker = CircuitBreaker(fail_threshold=5, reset_timeout=60.0)
    _shared_limiter = RateLimiter(qps=10.0, burst=20)