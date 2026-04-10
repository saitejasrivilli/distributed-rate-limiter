import redis.asyncio as redis
from redis.asyncio import ConnectionPool
import time
import os
import asyncio
from app.models import RateLimitResponse
from app.config import (
    REDIS_CONNECT_TIMEOUT, REDIS_SOCKET_TIMEOUT,
    REDIS_MAX_CONNECTIONS, REDIS_HEALTH_CHECK_INTERVAL,
)
from app.logging_config import logger

# ---------------------------------------------------------------------------
# Lua scripts — all operations are atomic (single round-trip to Redis).
# Using KEYS[] properly so Redis Cluster can route correctly.
# ---------------------------------------------------------------------------

# Sliding window: ZSET scored by timestamp-ms.
# Fixed unique member via counter suffix avoids collision at high concurrency
# (replaces math.random which has no seed and can produce duplicates).
SLIDING_WINDOW_SCRIPT = """
local key        = KEYS[1]
local counter_key = KEYS[2]
local now        = tonumber(ARGV[1])
local window_ms  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local cutoff     = now - window_ms

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)

local count = redis.call('ZCARD', key)

if count < limit then
    local seq = redis.call('INCR', counter_key)
    redis.call('PEXPIRE', counter_key, window_ms + 1000)
    local member = now .. '-' .. seq
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window_ms + 1000)
    return {1, limit - count - 1, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_ms = 0
    if #oldest > 0 then
        retry_ms = tonumber(oldest[2]) + window_ms - now
    end
    local retry_sec = math.max(1, math.ceil(retry_ms / 1000))
    return {0, 0, retry_sec}
end
"""

# Fixed window: single INCR on a time-bucketed key.
FIXED_WINDOW_SCRIPT = """
local key        = KEYS[1]
local limit      = tonumber(ARGV[1])
local window_sec = tonumber(ARGV[2])
local now_ms     = tonumber(ARGV[3])

local bucket     = math.floor(now_ms / (window_sec * 1000))
local full_key   = key .. ':' .. bucket

local count = redis.call('INCR', full_key)
if count == 1 then
    redis.call('EXPIRE', full_key, window_sec + 1)
end

if count <= limit then
    return {1, limit - count, 0}
else
    local ttl = redis.call('TTL', full_key)
    return {0, 0, math.max(1, ttl)}
end
"""

# Token bucket: stored as Redis Hash {tokens, last_refill}.
# Virtual refill on every call — no background worker needed.
TOKEN_BUCKET_SCRIPT = """
local key         = KEYS[1]
local capacity    = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms      = tonumber(ARGV[3])

local data        = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens      = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil or last_refill == nil then
    tokens      = capacity
    last_refill = now_ms
end

local elapsed_sec = (now_ms - last_refill) / 1000.0
if elapsed_sec < 0 then elapsed_sec = 0 end

local new_tokens = math.min(capacity, tokens + elapsed_sec * refill_rate)

local ttl_sec = math.ceil(capacity / refill_rate) + 60

if new_tokens >= 1.0 then
    new_tokens = new_tokens - 1.0
    redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill', now_ms)
    redis.call('EXPIRE', key, ttl_sec)
    return {1, math.floor(new_tokens), 0}
else
    local wait_sec = math.ceil((1.0 - new_tokens) / refill_rate)
    redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill', now_ms)
    redis.call('EXPIRE', key, ttl_sec)
    return {0, 0, math.max(1, wait_sec)}
end
"""

# Self-protection: used internally to rate-limit the API itself.
# Simple fixed-window used here for lowest overhead.
SELF_PROTECT_SCRIPT = """
local key        = KEYS[1]
local limit      = tonumber(ARGV[1])
local window_sec = tonumber(ARGV[2])
local now_ms     = tonumber(ARGV[3])

local bucket     = math.floor(now_ms / (window_sec * 1000))
local full_key   = key .. ':' .. bucket

local count = redis.call('INCR', full_key)
if count == 1 then
    redis.call('EXPIRE', full_key, window_sec + 1)
end

if count <= limit then
    return 1
else
    return 0
end
"""


class RateLimiter:
    def __init__(self):
        self._pool: ConnectionPool | None = None
        self._redis: redis.Redis | None = None
        self._sliding_script = None
        self._fixed_script = None
        self._token_script = None
        self._self_protect_script = None

    async def connect(self):
        url = os.getenv("UPSTASH_REDIS_URL", "redis://localhost:6379")

        self._pool = ConnectionPool.from_url(
            url,
            max_connections=REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            health_check_interval=REDIS_HEALTH_CHECK_INTERVAL,
            decode_responses=True,
            retry_on_timeout=True,
        )
        self._redis = redis.Redis(connection_pool=self._pool)

        # Verify connection at startup — fail fast
        await self._redis.ping()

        # Register scripts — SHA cached by redis-py
        self._sliding_script = self._redis.register_script(SLIDING_WINDOW_SCRIPT)
        self._fixed_script = self._redis.register_script(FIXED_WINDOW_SCRIPT)
        self._token_script = self._redis.register_script(TOKEN_BUCKET_SCRIPT)
        self._self_protect_script = self._redis.register_script(SELF_PROTECT_SCRIPT)

        logger.info("Redis connection pool established")

    async def close(self):
        if self._pool:
            await self._pool.aclose()
            logger.info("Redis connection pool closed")

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Self-protection — call before serving any rate-limit check
    # ------------------------------------------------------------------
    async def self_protect(self, key_suffix: str, limit: int, window: int) -> bool:
        """Returns True if allowed, False if the service itself is being flooded."""
        try:
            key = f"rl:_self:{key_suffix}"
            now_ms = int(time.time() * 1000)
            result = await self._self_protect_script(
                keys=[key], args=[limit, window, now_ms]
            )
            return int(result) == 1
        except Exception as exc:
            # Fail open on self-protect errors — don't block legitimate traffic
            logger.warning("self_protect error, failing open", extra={"exc_msg": str(exc)})
            return True

    # ------------------------------------------------------------------
    # Algorithm implementations
    # ------------------------------------------------------------------
    async def sliding_window(
        self, client_id: str, limit: int, window_seconds: int
    ) -> RateLimitResponse:
        key = f"rl:sw:{client_id}"
        counter_key = f"rl:sw:seq:{client_id}"
        now_ms = int(time.time() * 1000)
        try:
            result = await self._sliding_script(
                keys=[key, counter_key],
                args=[now_ms, window_seconds * 1000, limit],
            )
        except Exception as exc:
            logger.error(
                "sliding_window redis error — failing open",
                extra={"client_id": client_id, "exc_msg": str(exc)},
            )
            # Fail open: allow the request, log the error
            return RateLimitResponse(
                allowed=True, remaining=limit, limit=limit,
                retry_after=None, algorithm="sliding_window", client_id=client_id,
            )
        allowed, remaining, retry_after = int(result[0]), int(result[1]), int(result[2])
        return RateLimitResponse(
            allowed=bool(allowed),
            remaining=remaining,
            limit=limit,
            retry_after=retry_after if not allowed else None,
            algorithm="sliding_window",
            client_id=client_id,
        )

    async def fixed_window(
        self, client_id: str, limit: int, window_seconds: int
    ) -> RateLimitResponse:
        key = f"rl:fw:{client_id}"
        now_ms = int(time.time() * 1000)
        try:
            result = await self._fixed_script(
                keys=[key],
                args=[limit, window_seconds, now_ms],
            )
        except Exception as exc:
            logger.error(
                "fixed_window redis error — failing open",
                extra={"client_id": client_id, "exc_msg": str(exc)},
            )
            return RateLimitResponse(
                allowed=True, remaining=limit, limit=limit,
                retry_after=None, algorithm="fixed_window", client_id=client_id,
            )
        allowed, remaining, retry_after = int(result[0]), int(result[1]), int(result[2])
        return RateLimitResponse(
            allowed=bool(allowed),
            remaining=remaining,
            limit=limit,
            retry_after=retry_after if not allowed else None,
            algorithm="fixed_window",
            client_id=client_id,
        )

    async def token_bucket(
        self, client_id: str, capacity: int, refill_rate: float
    ) -> RateLimitResponse:
        key = f"rl:tb:{client_id}"
        now_ms = int(time.time() * 1000)
        try:
            result = await self._token_script(
                keys=[key],
                args=[capacity, refill_rate, now_ms],
            )
        except Exception as exc:
            logger.error(
                "token_bucket redis error — failing open",
                extra={"client_id": client_id, "exc_msg": str(exc)},
            )
            return RateLimitResponse(
                allowed=True, remaining=capacity, limit=capacity,
                retry_after=None, algorithm="token_bucket", client_id=client_id,
            )
        allowed, remaining, retry_after = int(result[0]), int(result[1]), int(result[2])
        return RateLimitResponse(
            allowed=bool(allowed),
            remaining=remaining,
            limit=capacity,
            retry_after=retry_after if not allowed else None,
            algorithm="token_bucket",
            client_id=client_id,
        )

    async def reset(self, client_id: str) -> int:
        """
        Delete all state for a client_id.
        Uses explicit key patterns instead of KEYS * scan to avoid O(N) blocking.
        Fixed-window keys are time-bucketed — we delete the last 48 buckets
        to cover any reasonable window size without a full keyspace scan.
        """
        now_ms = int(time.time() * 1000)

        # Explicit keys for sliding window and token bucket
        keys_to_delete = [
            f"rl:sw:{client_id}",
            f"rl:sw:seq:{client_id}",
            f"rl:tb:{client_id}",
        ]

        # Fixed window: generate bucket keys for last 48 windows of each size
        # (covers windows from 1s up to 86400s without KEYS scan)
        window_sizes = [1, 5, 10, 30, 60, 300, 600, 3600, 86400]
        for ws in window_sizes:
            window_ms = ws * 1000
            current_bucket = now_ms // window_ms
            for offset in range(48):
                bucket = current_bucket - offset
                keys_to_delete.append(f"rl:fw:{client_id}:{bucket}")

        # Pipeline the deletes — single round-trip
        pipe = self._redis.pipeline(transaction=False)
        for k in keys_to_delete:
            pipe.delete(k)
        results = await pipe.execute()
        return sum(1 for r in results if r)
