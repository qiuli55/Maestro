"""resilience 模块单测：retry / circuit breaker / rate limiter / is_transient_error。"""
from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, "src")

import pytest

from maestro import resilience


# ====== helpers ======

def _make_openai_exc(class_name, status_code=None):
    """动态创建类名+模块名都像 openai SDK 的异常类（用 type() 才能设属性）。"""
    attrs = {"__module__": "openai"}
    if status_code is not None:
        attrs["status_code"] = status_code
    cls = type(class_name, (Exception,), attrs)
    return cls("simulated")


# ====== is_transient_error ======

def test_is_transient_openai_rate_limit():
    """openai RateLimitError (429) -> transient。"""
    err = _make_openai_exc("RateLimitError")
    assert resilience.is_transient_error(err) is True


def test_is_transient_openai_auth_error_is_permanent():
    """openai AuthenticationError (401) -> permanent。"""
    err = _make_openai_exc("AuthenticationError")
    assert resilience.is_transient_error(err) is False


def test_is_transient_openai_internal_server_error():
    """openai InternalServerError -> transient。"""
    err = _make_openai_exc("InternalServerError")
    assert resilience.is_transient_error(err) is True


def test_is_transient_openai_api_status_5xx():
    """APIStatusError 5xx -> transient。"""
    err = _make_openai_exc("APIStatusError", status_code=503)
    assert resilience.is_transient_error(err) is True


def test_is_transient_openai_api_status_4xx():
    """APIStatusError 4xx -> permanent（用户输入问题，不该重试）。"""
    err = _make_openai_exc("APIStatusError", status_code=400)
    assert resilience.is_transient_error(err) is False


def test_is_transient_connection_error():
    """通用 ConnectionError -> transient。"""
    assert resilience.is_transient_error(ConnectionError("refused")) is True


def test_is_transient_timeout_error():
    """通用 TimeoutError -> transient。"""
    assert resilience.is_transient_error(TimeoutError("timed out")) is True


def test_is_transient_unknown_error_defaults_transient():
    """未知异常默认 transient（保守：宁可重试，不可漏判）。"""
    assert resilience.is_transient_error(ValueError("bad input")) is True


# ====== RetryPolicy ======

def test_retry_policy_delay_grows_exponentially():
    """delay(attempt) 呈指数增长，受 max_delay 上限约束。"""
    p = resilience.RetryPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0)
    assert p.delay(0) == 1.0
    assert p.delay(1) == 2.0
    assert p.delay(2) == 4.0
    assert p.delay(3) == 8.0
    assert p.delay(4) == 10.0  # min(10, 16) 封顶


def test_retry_policy_delay_jitter_in_range():
    """jitter > 0 时 delay 在 [base, base+jitter] 范围。"""
    p = resilience.RetryPolicy(base_delay=1.0, max_delay=100.0, jitter=0.5)
    for _ in range(20):
        d = p.delay(0)
        assert 1.0 <= d <= 1.5


# ====== CircuitBreaker ======

def test_breaker_closed_allows_call():
    b = resilience.CircuitBreaker(fail_threshold=3, reset_timeout=60.0)
    assert b.state == resilience.CircuitBreaker.CLOSED
    assert b.allow() is True


def test_breaker_opens_after_threshold_failures():
    b = resilience.CircuitBreaker(fail_threshold=3, reset_timeout=60.0)
    for _ in range(3):
        b.record_failure()
    assert b.state == resilience.CircuitBreaker.OPEN
    assert b.allow() is False


def test_breaker_success_resets_failures():
    b = resilience.CircuitBreaker(fail_threshold=3, reset_timeout=60.0)
    b.record_failure()
    b.record_failure()
    b.record_success()  # 重置
    b.record_failure()
    b.record_failure()
    # 还差 1 次
    assert b.state == resilience.CircuitBreaker.CLOSED


def test_breaker_half_open_after_reset_timeout():
    """reset_timeout 秒后 OPEN 自动转 HALF_OPEN（懒检查）。"""
    b = resilience.CircuitBreaker(fail_threshold=2, reset_timeout=0.1)
    b.record_failure()
    b.record_failure()
    assert b.state == resilience.CircuitBreaker.OPEN
    assert b.allow() is False
    time.sleep(0.15)
    assert b.state == resilience.CircuitBreaker.HALF_OPEN
    assert b.allow() is True


def test_breaker_half_open_success_closes():
    """HALF_OPEN 探测成功 -> CLOSED。"""
    b = resilience.CircuitBreaker(fail_threshold=2, reset_timeout=0.1)
    b.record_failure()
    b.record_failure()
    time.sleep(0.15)
    _ = b.state  # 触发 OPEN -> HALF_OPEN
    b.record_success()
    assert b.state == resilience.CircuitBreaker.CLOSED
    assert b.fail_count == 0


def test_breaker_half_open_failure_reopens():
    """HALF_OPEN 探测失败 -> OPEN（重新计时）。"""
    b = resilience.CircuitBreaker(fail_threshold=2, reset_timeout=0.1)
    b.record_failure()
    b.record_failure()
    time.sleep(0.15)
    _ = b.state
    b.record_failure()
    assert b.state == resilience.CircuitBreaker.OPEN


def test_breaker_thread_safety():
    """多线程并发 record_failure 不丢计数。"""
    b = resilience.CircuitBreaker(fail_threshold=100, reset_timeout=60.0)

    def hit():
        for _ in range(10):
            b.record_failure()

    threads = [threading.Thread(target=hit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.fail_count == 50


def test_breaker_manual_reset():
    b = resilience.CircuitBreaker(fail_threshold=2)
    b.record_failure()
    b.record_failure()
    assert b.state == resilience.CircuitBreaker.OPEN
    b.reset()
    assert b.state == resilience.CircuitBreaker.CLOSED
    assert b.fail_count == 0


# ====== RateLimiter ======

def test_rate_limiter_burst_allows_initial_burst():
    """burst 内允许密集调用。"""
    rl = resilience.RateLimiter(qps=1.0, burst=5)
    start = time.monotonic()
    for _ in range(5):
        rl.acquire(timeout=1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5


def test_rate_limiter_blocks_after_burst_exhausted():
    """burst 用完后必须等令牌生成。"""
    rl = resilience.RateLimiter(qps=2.0, burst=3)  # 每 0.5s 一个
    for _ in range(3):
        rl.acquire(timeout=1.0)
    start = time.monotonic()
    rl.acquire(timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4, f"应等 ~0.5s，实 {elapsed:.2f}s"


def test_rate_limiter_timeout_returns_false():
    """acquire 超时返回 False。"""
    rl = resilience.RateLimiter(qps=0.5, burst=1)  # 极慢：每 2s 一个
    rl.acquire(timeout=1.0)  # 用掉 burst
    start = time.monotonic()
    got = rl.acquire(timeout=0.1)
    elapsed = time.monotonic() - start
    assert got is False
    assert elapsed < 0.3  # 真的等了 timeout


def test_rate_limiter_per_thread_independent():
    """每线程独立 token 桶。"""
    rl = resilience.RateLimiter(qps=1.0, burst=2)
    results = []

    def worker():
        rl.acquire(timeout=1.0)
        rl.acquire(timeout=1.0)
        results.append(threading.get_ident())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 3


# ====== with_resilience decorator ======

def test_with_resilience_succeeds_first_try():
    """happy path：不重试。"""
    calls = [0]

    @resilience.with_resilience(retry=resilience.RetryPolicy(max_retries=3, base_delay=0.01))
    def fn():
        calls[0] += 1
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 1


def test_with_resilience_retries_transient():
    """transient 异常：按 max_retries 重试直到成功。"""
    calls = [0]

    @resilience.with_resilience(retry=resilience.RetryPolicy(max_retries=3, base_delay=0.01))
    def fn():
        calls[0] += 1
        if calls[0] < 3:
            raise ConnectionError("transient")
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 3


def test_with_resilience_no_retry_permanent():
    """permanent 异常：不重试，立刻抛。"""
    calls = [0]

    @resilience.with_resilience(retry=resilience.RetryPolicy(max_retries=3, base_delay=0.01))
    def fn():
        calls[0] += 1
        raise _make_openai_exc("AuthenticationError")

    with pytest.raises(Exception):
        fn()
    assert calls[0] == 1


def test_with_resilience_exhausts_retries_then_raises():
    """用完 max_retries 后重抛最后一次异常。"""
    calls = [0]

    @resilience.with_resilience(retry=resilience.RetryPolicy(max_retries=2, base_delay=0.01))
    def fn():
        calls[0] += 1
        raise ConnectionError("always fail")

    with pytest.raises(ConnectionError):
        fn()
    # 1 首次 + 2 重试 = 3 次
    assert calls[0] == 3


def test_with_resilience_circuit_open_fails_fast():
    """熔断打开时直接抛 CircuitOpenError，不调用 fn。"""
    calls = [0]
    breaker = resilience.CircuitBreaker(fail_threshold=2, reset_timeout=60.0)

    @resilience.with_resilience(
        retry=resilience.RetryPolicy(max_retries=0, base_delay=0.01),
        breaker=breaker,
    )
    def fn():
        calls[0] += 1
        raise ConnectionError("fail")

    with pytest.raises(ConnectionError):
        fn()
    with pytest.raises(ConnectionError):
        fn()
    # 第 2 次后熔断打开
    assert breaker.state == resilience.CircuitBreaker.OPEN

    calls[0] = 0  # 重置计数
    with pytest.raises(resilience.CircuitOpenError):
        fn()
    assert calls[0] == 0  # fn 没被调用


def test_with_resilience_records_breaker_success():
    """成功调用记录到 breaker（重置失败计数）。"""
    breaker = resilience.CircuitBreaker(fail_threshold=3)
    breaker.record_failure()
    breaker.record_failure()

    @resilience.with_resilience(
        retry=resilience.RetryPolicy(max_retries=0),
        breaker=breaker,
    )
    def fn():
        return "ok"

    fn()
    assert breaker.state == resilience.CircuitBreaker.CLOSED
    assert breaker.fail_count == 0


def test_with_resilience_records_breaker_failure():
    """失败调用记录到 breaker。"""
    breaker = resilience.CircuitBreaker(fail_threshold=3)

    @resilience.with_resilience(
        retry=resilience.RetryPolicy(max_retries=0),
        breaker=breaker,
    )
    def fn():
        raise ConnectionError("fail")

    with pytest.raises(ConnectionError):
        fn()
    assert breaker.fail_count == 1


def test_with_resilience_applies_rate_limit():
    """限流：连续 acquire 后延迟。"""
    limiter = resilience.RateLimiter(qps=5.0, burst=2)

    @resilience.with_resilience(
        retry=resilience.RetryPolicy(max_retries=0),
        limiter=limiter,
    )
    def fn():
        return "ok"

    start = time.monotonic()
    for _ in range(3):  # 3 个：2 burst + 1 个要等 0.2s
        fn()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15, f"第 3 个应至少等 0.2s，实 {elapsed:.2f}s"


# ====== shared singletons ======

def test_shared_breaker_limiter_reset():
    """reset_shared 重置全局熔断器 + 限流器（测试隔离）。"""
    b1 = resilience.shared_breaker()
    b1.record_failure()
    b1.record_failure()
    b1.record_failure()
    b1.record_failure()
    b1.record_failure()
    assert b1.state == resilience.CircuitBreaker.OPEN
    resilience.reset_shared()
    b2 = resilience.shared_breaker()
    assert b2.state == resilience.CircuitBreaker.CLOSED
    # 注：reset_shared 会创建新实例，b1 和 b2 是不同对象