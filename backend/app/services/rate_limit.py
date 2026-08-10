"""B 站请求全局限流器（参考 bilibinggo src/bilibili_rate_limit.py）

bilibinggo 方案：
- 进程内 TokenBucketRateLimiter（令牌桶，线程安全）
- 默认 3 RPS（BILI_RPS 环境变量可调），每个 API 请求前 acquire()
- 动作链每步之间再 sleep ACTION_INTERVAL_SEC(1.5s)

本项目：同样全局令牌桶 + 动作间固定间隔（1.2s），供 bili_actions 请求调用。
"""
import threading
import time

DEFAULT_BILI_RPS = 3.0        # 每秒最多 3 个 B 站请求
ACTION_GAP_SEC = 1.2          # 动作链每步间隔（点赞/转发/评论之间）


class TokenBucketRateLimiter:
    """进程内全局限流：令牌桶，线程安全（对齐 bilibinggo）"""

    def __init__(self, rps: float, *, burst: float | None = None) -> None:
        self._rps = max(0.0, float(rps))
        self._capacity = max(1.0, float(burst if burst is not None else min(2.0, self._rps)))
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self._rps <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill)
                self._last_refill = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rps)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_sec = (1.0 - self._tokens) / self._rps
            time.sleep(wait_sec)


_limiter_lock = threading.Lock()
_limiter: TokenBucketRateLimiter | None = None


def get_bili_rate_limiter() -> TokenBucketRateLimiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = TokenBucketRateLimiter(DEFAULT_BILI_RPS)
        return _limiter


def configure_rate_limit(rps: float | None = None) -> None:
    """按设置项动态调整限流速率（rps<=0 关闭限流）"""
    global _limiter
    with _limiter_lock:
        resolved = DEFAULT_BILI_RPS if rps is None else max(0.0, float(rps))
        if _limiter is None:
            _limiter = TokenBucketRateLimiter(resolved)
        elif abs(_limiter._rps - resolved) > 0.001:
            # 保持令牌存量，仅更新速率（burst 同步）
            _limiter._rps = resolved
            _limiter._capacity = max(1.0, min(2.0, resolved))


def acquire_bili_request_slot() -> None:
    """每个 B 站 API 请求前调用，全局限流"""
    get_bili_rate_limiter().acquire()
