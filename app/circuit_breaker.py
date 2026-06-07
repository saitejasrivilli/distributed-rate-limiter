"""
Circuit breaker for Redis failures.

States:
  CLOSED   — normal operation, all calls go to Redis
  OPEN     — Redis is down; in-memory token bucket serves requests
  HALF_OPEN — probe period after recovery timeout; one test call allowed

Transition rules:
  CLOSED  → OPEN      : >= FAILURE_THRESHOLD failures in FAILURE_WINDOW seconds
  OPEN    → HALF_OPEN : RECOVERY_TIMEOUT seconds have elapsed
  HALF_OPEN → CLOSED  : probe call succeeds
  HALF_OPEN → OPEN    : probe call fails (reset recovery timer)
"""

import threading
import time
import collections
from enum import Enum
from app.logging_config import logger

FAILURE_THRESHOLD = 5       # failures to open the breaker
FAILURE_WINDOW = 10.0       # seconds over which failures are counted
RECOVERY_TIMEOUT = 30.0     # seconds to wait before trying HALF_OPEN


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class InMemoryTokenBucket:
    """
    Thread-safe per-client token bucket for in-memory fallback.
    Uses a simple dict protected by a single lock.
    Suitable for single-instance fallback; not distributed.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # {client_key: {"tokens": float, "last_refill": float}}
        self._buckets: dict[str, dict] = {}

    def check(self, key: str, capacity: int, refill_rate: float) -> tuple[bool, int]:
        """
        Returns (allowed, remaining).
        capacity  — max tokens
        refill_rate — tokens per second
        """
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = {"tokens": float(capacity), "last_refill": now}
                self._buckets[key] = bucket

            elapsed = max(0.0, now - bucket["last_refill"])
            new_tokens = min(float(capacity), bucket["tokens"] + elapsed * refill_rate)
            bucket["last_refill"] = now

            if new_tokens >= 1.0:
                new_tokens -= 1.0
                bucket["tokens"] = new_tokens
                return True, int(new_tokens)
            else:
                bucket["tokens"] = new_tokens
                return False, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


class CircuitBreaker:
    """
    Thread-safe circuit breaker wrapping Redis calls.

    Usage:
        cb = CircuitBreaker()

        async def call_redis():
            ...

        result = await cb.call(call_redis)  # raises if OPEN
    """

    def __init__(
        self,
        failure_threshold: int = FAILURE_THRESHOLD,
        failure_window: float = FAILURE_WINDOW,
        recovery_timeout: float = RECOVERY_TIMEOUT,
    ):
        self._failure_threshold = failure_threshold
        self._failure_window = failure_window
        self._recovery_timeout = recovery_timeout

        self._lock = threading.Lock()
        self._state = State.CLOSED
        # deque of timestamps of recent failures
        self._failure_times: collections.deque[float] = collections.deque()
        self._opened_at: float = 0.0

        # Shared in-memory fallback bucket store
        self.fallback = InMemoryTokenBucket()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state == State.HALF_OPEN:
                logger.info("circuit_breaker: probe succeeded — closing")
                self._state = State.CLOSED
                self._failure_times.clear()

    def record_failure(self) -> None:
        now = time.time()
        with self._lock:
            # Prune old failures outside the window
            cutoff = now - self._failure_window
            while self._failure_times and self._failure_times[0] < cutoff:
                self._failure_times.popleft()

            self._failure_times.append(now)

            if self._state == State.HALF_OPEN:
                logger.warning("circuit_breaker: probe failed — reopening")
                self._state = State.OPEN
                self._opened_at = now
                return

            if (
                self._state == State.CLOSED
                and len(self._failure_times) >= self._failure_threshold
            ):
                logger.error(
                    "circuit_breaker: threshold reached — opening",
                    extra={
                        "failures_in_window": len(self._failure_times),
                        "window_sec": self._failure_window,
                    },
                )
                self._state = State.OPEN
                self._opened_at = now

    def allow_request(self) -> bool:
        """
        Returns True if the call should be forwarded to Redis.
        Handles OPEN → HALF_OPEN transition.
        """
        now = time.time()
        with self._lock:
            if self._state == State.CLOSED:
                return True

            if self._state == State.OPEN:
                if now - self._opened_at >= self._recovery_timeout:
                    logger.info("circuit_breaker: recovery timeout elapsed — moving to HALF_OPEN")
                    self._state = State.HALF_OPEN
                    return True  # allow the probe
                return False  # still open

            # HALF_OPEN — only one probe at a time; block subsequent callers
            return False

    async def call(self, coro_fn, *args, **kwargs):
        """
        Execute ``coro_fn(*args, **kwargs)``, recording success/failure.
        Raises ``RedisCircuitOpen`` when the breaker is OPEN and not probing.
        """
        if not self.allow_request():
            raise RedisCircuitOpen("Circuit breaker is OPEN — Redis is unavailable")

        try:
            result = await coro_fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise


class RedisCircuitOpen(Exception):
    """Raised when the circuit breaker is open and blocks a Redis call."""


# Module-level singleton used by limiter.py
circuit_breaker = CircuitBreaker()
