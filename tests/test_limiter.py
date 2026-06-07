import asyncio
import os
import time
import threading
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.limiter import (
    RateLimiter,
    SLIDING_WINDOW_SCRIPT, FIXED_WINDOW_SCRIPT,
    TOKEN_BUCKET_SCRIPT, LEAKY_BUCKET_SCRIPT, SELF_PROTECT_SCRIPT,
)
from app.circuit_breaker import CircuitBreaker, InMemoryTokenBucket, State

REDIS_URL = os.getenv("UPSTASH_REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture
async def real_redis():
    client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def _wire_limiter(rl: RateLimiter, r: aioredis.Redis) -> RateLimiter:
    rl._redis = r
    rl._pool = None
    rl._sliding_script = r.register_script(SLIDING_WINDOW_SCRIPT)
    rl._fixed_script = r.register_script(FIXED_WINDOW_SCRIPT)
    rl._token_script = r.register_script(TOKEN_BUCKET_SCRIPT)
    rl._leaky_script = r.register_script(LEAKY_BUCKET_SCRIPT)
    rl._self_protect_script = r.register_script(SELF_PROTECT_SCRIPT)
    return rl


@pytest_asyncio.fixture
async def limiter(real_redis):
    yield _wire_limiter(RateLimiter(), real_redis)


@pytest_asyncio.fixture
async def client(real_redis):
    from app import circuit_breaker as cb_module
    cb_module.circuit_breaker._state = State.CLOSED
    cb_module.circuit_breaker._failure_times.clear()
    cb_module.circuit_breaker.fallback.clear()

    import app.main as main_module
    main_module.limiter = _wire_limiter(RateLimiter(), real_redis)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# 1. Sliding window — allow under limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sliding_window_allows_under_limit(limiter):
    cid = "sw_under_test"
    limit = 5
    window = 60

    results = []
    for _ in range(limit):
        r = await limiter.sliding_window(cid, limit, window)
        results.append(r)

    assert all(r.allowed for r in results), "All requests under limit should be allowed"
    assert results[-1].remaining == 0, "Last request should leave 0 remaining"
    assert results[0].remaining == limit - 1


# ---------------------------------------------------------------------------
# 2. Sliding window — reject over limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sliding_window_rejects_over_limit(limiter):
    cid = "sw_over_test"
    limit = 3
    window = 60

    responses = [await limiter.sliding_window(cid, limit, window) for _ in range(limit + 2)]

    allowed = [r for r in responses if r.allowed]
    blocked = [r for r in responses if not r.allowed]

    assert len(allowed) == limit
    assert len(blocked) == 2
    for b in blocked:
        assert b.retry_after is not None
        assert b.retry_after >= 1


# ---------------------------------------------------------------------------
# 3. Fixed window — resets after window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fixed_window_resets_after_window(limiter):
    cid = "fw_reset_test"
    limit = 3
    window = 1

    for _ in range(limit):
        r = await limiter.fixed_window(cid, limit, window)
        assert r.allowed

    blocked = await limiter.fixed_window(cid, limit, window)
    assert not blocked.allowed
    assert blocked.retry_after is not None
    assert blocked.retry_after >= 1


# ---------------------------------------------------------------------------
# 4. Token bucket — allows burst
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_bucket_allows_burst(limiter):
    cid = "tb_burst_test"
    capacity = 8
    refill_rate = 1.0

    results = [await limiter.token_bucket(cid, capacity, refill_rate) for _ in range(capacity)]

    allowed = [r for r in results if r.allowed]
    assert len(allowed) == capacity, "Should allow full burst equal to capacity"

    rejected = await limiter.token_bucket(cid, capacity, refill_rate)
    assert not rejected.allowed
    assert rejected.retry_after is not None


# ---------------------------------------------------------------------------
# 5. Leaky bucket — smooths traffic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leaky_bucket_smooths_traffic(limiter):
    cid = "lb_smooth_test"
    capacity = 5
    leak_rate = 1.0

    results = [await limiter.leaky_bucket(cid, capacity, leak_rate) for _ in range(capacity)]
    allowed = [r for r in results if r.allowed]
    assert len(allowed) == capacity, "Queue should accept up to capacity"

    rejected = await limiter.leaky_bucket(cid, capacity, leak_rate)
    assert not rejected.allowed
    assert rejected.retry_after is not None
    assert rejected.retry_after >= 1


# ---------------------------------------------------------------------------
# 6. Circuit breaker — opens on Redis failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_opens_on_redis_failure():
    cb = CircuitBreaker(failure_threshold=3, failure_window=10.0, recovery_timeout=30.0)

    assert cb.state == State.CLOSED

    for _ in range(3):
        cb.record_failure()

    assert cb.state == State.OPEN, "Should open after threshold failures"
    assert not cb.allow_request(), "OPEN breaker should block requests"


# ---------------------------------------------------------------------------
# 7. Circuit breaker — falls back to in-memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_falls_back_to_memory(limiter):
    from app import circuit_breaker as cb_module

    original_state = cb_module.circuit_breaker._state
    cb_module.circuit_breaker._state = State.OPEN
    cb_module.circuit_breaker._opened_at = time.time()

    try:
        result = await limiter.sliding_window("cb_fallback_user", 10, 60)
        assert isinstance(result.allowed, bool)
        assert result.algorithm == "sliding_window"
    finally:
        cb_module.circuit_breaker._state = original_state


# ---------------------------------------------------------------------------
# 8. HTTP endpoint — returns 429 with Retry-After header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after(client):
    cid = "http_429_test"
    limit = 2
    window = 60

    for _ in range(limit):
        resp = await client.get(
            "/check/sliding_window",
            params={"client_id": cid, "limit": limit, "window_seconds": window},
        )
        assert resp.status_code == 200

    resp = await client.get(
        "/check/sliding_window",
        params={"client_id": cid, "limit": limit, "window_seconds": window},
    )
    assert resp.status_code == 429
    assert "retry_after_seconds" in resp.json()["detail"]
    assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# 9. Security — rejects malicious client_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_security_rejects_malicious_client_id(client):
    malicious_ids = [
        "user*",
        "user:../../../admin",
        "a" * 200,
        "",
        "user\x00admin",
    ]

    for bad_id in malicious_ids:
        resp = await client.get(
            "/check/sliding_window",
            params={"client_id": bad_id, "limit": 10, "window_seconds": 60},
        )
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for malicious client_id {bad_id!r}, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 10. Leaky bucket HTTP endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leaky_bucket_http_endpoint(client):
    resp = await client.get(
        "/check/leaky_bucket",
        params={"client_id": "lb_http_test", "capacity": 5, "leak_rate": 1.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["algorithm"] == "leaky_bucket"
    assert "remaining" in body
    assert "allowed" in body


# ---------------------------------------------------------------------------
# 11. Health endpoint includes circuit breaker state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_includes_circuit_breaker_state(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "circuit_breaker_state" in body
    assert body["circuit_breaker_state"] in ("closed", "open", "half_open")


# ---------------------------------------------------------------------------
# 12. Metrics endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "# HELP" in body or "# TYPE" in body or "rate_limit" in body


# ---------------------------------------------------------------------------
# 13. In-memory token bucket thread safety
# ---------------------------------------------------------------------------

def test_in_memory_token_bucket_thread_safety():
    bucket = InMemoryTokenBucket()
    results = []
    lock = threading.Lock()

    def consume():
        for _ in range(20):
            allowed, _ = bucket.check("shared_client", capacity=10, refill_rate=100.0)
            with lock:
                results.append(allowed)

    threads = [threading.Thread(target=consume) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 200


# ---------------------------------------------------------------------------
# 14. Circuit breaker half-open → closed on success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_half_open_closes_on_success():
    cb = CircuitBreaker(failure_threshold=2, failure_window=10.0, recovery_timeout=0.1)

    cb.record_failure()
    cb.record_failure()
    assert cb.state == State.OPEN

    await asyncio.sleep(0.15)

    assert cb.allow_request() is True
    assert cb.state == State.HALF_OPEN

    cb.record_success()
    assert cb.state == State.CLOSED
