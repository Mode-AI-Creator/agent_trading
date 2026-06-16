from __future__ import annotations

import asyncio
import threading
import time


class TokenBucket:
    """Thread-safe token bucket for rate limiting synchronous code."""

    def __init__(self, rate: float, capacity: float) -> None:
        """
        Args:
            rate: tokens replenished per second
            capacity: maximum tokens (burst capacity)
        """
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, block: bool = True) -> bool:
        """Acquire tokens. If block=True, sleeps until tokens are available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
            if not block:
                return False
            time.sleep(0.05)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


class AsyncTokenBucket:
    """Async-compatible token bucket for use with asyncio."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Acquires tokens, sleeping if necessary."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
            await asyncio.sleep(0.05)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


# Pre-configured limiters for known APIs
# OKX REST: 20 requests per 2 seconds per endpoint
okx_rest_limiter = AsyncTokenBucket(rate=10.0, capacity=20.0)

# Twitter v2: conservative defaults (varies by tier)
twitter_limiter = AsyncTokenBucket(rate=0.5, capacity=5.0)

# DeepSeek: 60 RPM → 1 RPS，宽松限速
deepseek_limiter = AsyncTokenBucket(rate=1.0, capacity=5.0)
