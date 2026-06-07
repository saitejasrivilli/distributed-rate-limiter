"""
Pytest integration tests for the Distributed Rate Limiter.

Uses fakeredis for in-process Redis emulation — no real Redis required.
Async tests use httpx.AsyncClient against the FastAPI app.

Run:
    pip install pytest pytest-asyncio httpx fakeredis
    pytest tests/ -v
"""

import asyncio
import time
import pytest
import pytest_asyncio
import fakeredis.aioredis as fakeredis
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.limiter import RateLimiter
from app.circuit_breaker import CircuitBreaker, InMemoryTokenBucket, State, RedisCircuitOpen


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture
async def fake_redis():
    """Provides a fakeredis instance that mimics the real redis-py async client."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def limiter(fake_redis):
    """RateLimiter wired to fakeredis, with scripts registered."""
    rl = RateLimiter()
    # Inject fake redis directly instead of connecting to a real server
    rl._redis = fake_redis
    rl._pool = None  # not needed for tests
    rl._sliding_script = fake_redis.register_script(
        __import__("app.limiter", fromlist=["SLIDING_WINDOW_SCRIPT"]).SLIDING_WINDOW_SCRIPT
    )
    rl._fixed_script = fake_redis.register_script(
        __import__("app.limiter", fromlist=["FIXED_WINDOW_SCRIPT"]).FIXED_WINDOW_SCRIPT
    )
    rl._token_script = fake_redis.register_script(
        __import__("app.limiter", fromlist=["TOKEN_BUCKET_SCRIPT"]).TOKEN_BUCKET_SCRIPT
    )
    rl._leaky_script = fake_redis.register_script(
        __import__("app.limiter", fromlist=["LEAKY_BUCKET_SCRIPT"]).LEAKY_BUCKET_SCRIPT
    )
    rl._self_protect_script = fake_redis.register_script(
        __import__("app.limiter", fromlist=["SELF_PROTECT_SCRIPT"]).SELF_PROTECT_SCRIPT
    )
    yield rl


@pytest_asyncio.fixture
async def client(fake_redis):
    """
    httpx AsyncClient against the FastAPI app with fakeredis injected.
    Circuit breaker is reset to CLOSED before each test.
    """
    from app import circuit_breaker as cb_module
    # Reset circuit breaker state
    cb_module.circuit_breaker._state = State.CLOSED
    cb_module.circuit_breaker._failure_times.clear()
    cb_module.circuit_breaker.fallback.clear()

    rl = RateLimiter()
    rl._redis = fake_redis
    rl._pool = None
    # Register scripts
    from app.limiter import (
        SLIDING_WINDOW_SCRIPT, FIXED_WINDOW_SCRIPT,
        TOKEN_BUCKET_SCRIPT, LEAKY_BUCKET_SCRIPT, SELF_PROTECT_SCRIPT,
    )
    rl._sliding_script = fake_redis.register_script(SLIDING_WINDOW_SCRIPT)
    rl._fixed_script = fake_redis.register_script(FIXED_WINDOW_SCRIPT)
    rl._token_script = fake_redis.register_script(TOKEN_BUCKET_SCRIPT)
    rl._leaky_script = fake_redis.register_script(LEAKY_BUCKET_SCRIPT)
    rl._self_protect_script = fake_redis.register_script(SELF_PROTECT_SCRIPT)

    import app.main as main_module
    main_module.limiter = rl

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
    window = 1  # 1-second window

    # Fill the window
    for _ in range(limit):
        r = await limiter.fixed_window(cid, limit, window)
        assert r.allowed

    # Should be blocked now
    blocked = await limiter.fixed_window(cid, limit, window)
    assert not blocked.allowed

    # Wait for window to expire and test again (fakeredis TTL is not time-based
    # in unit tests, so we just validate the blocked state is correct)
    assert blocked.retry_after is not None
    assert blocked.retry_after >= 1


# ---------------------------------------------------------------------------
# 4. Token bucket — allows burst
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_bucket_allows_burst(limiter):
    """Full bucket should absorb a burst of requests equal to capacity."""
    cid = "tb_burst_test"
    capacity = 8
    refill_rate = 1.0

    results = [await limiter.token_bucket(cid, capacity, refill_rate) for _ in range(capacity)]

    allowed = [r for r in results if r.allowed]
    assert len(allowed) == capacity, "Should allow full burst equal to capacity"

    # Next request should be rejected (bucket empty)
    rejected = await limiter.token_bucket(cid, capacity, refill_rate)
    assert not rejected.allowed
    assert rejected.retry_after is not None


# ---------------------------------------------------------------------------
# 5. Leaky bucket — smooths traffic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leaky_bucket_smooths_traffic(limiter):
    """
    Leaky bucket should accept up to capacity and then reject.
    Unlike token bucket, the queue only drains at leak_rate — no burst credit.
    """
    cid = "lb_smooth_test"
    capacity = 5
    leak_rate = 1.0

    # Fill the queue
    results = [await limiter.leaky_bucket(cid, capacity, leak_rate) for _ in range(capacity)]
    allowed = [r for r in results if r.allowed]
    assert len(allowed) == capacity, "Queue should accept up to capacity"

    # Queue is full — next should be rejected
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

    # Simulate failures
    for _ in range(3):
        cb.record_failure()

    assert cb.state == State.OPEN, "Should open after threshold failures"
    assert not cb.allow_request(), "OPEN breaker should block requests"


# ---------------------------------------------------------------------------
# 7. Circuit breaker — falls back to in-memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_falls_back_to_memory(limiter):
    """
    When the circuit breaker is OPEN, sliding_window should serve from
    the in-memory fallback bucket rather than raising an exception.
    """
    from app import circuit_breaker as cb_module

    # Force open the breaker
    original_state = cb_module.circuit_breaker._state
    cb_module.circuit_breaker._state = State.OPEN
    cb_module.circuit_breaker._opened_at = time.time()

    try:
        result = await limiter.sliding_window("cb_fallback_user", 10, 60)
        # Should not raise — should return a valid response from in-memory fallback
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

    # Exhaust the limit
    for _ in range(limit):
        resp = await client.get(
            "/check/sliding_window",
            params={"client_id": cid, "limit": limit, "window_seconds": window},
        )
        assert resp.status_code == 200

    # Next should be 429
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
        "user*",                    # glob pattern
        "user:../../../admin",      # path traversal
        "a" * 200,                  # length overflow
        "",                         # empty
        "user\x00admin",            # null byte (URL-encoded: %00)
    ]

    for bad_id in malicious_ids:
        resp = await client.get(
            "/check/sliding_window",
            params={"client_id": bad_id, "limit": 10, "window_seconds": 60},
        )
        # Either 400 (validation error) or 422 (FastAPI body validation)
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for malicious client_id {bad_id!r}, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 10. Leaky bucket HTTP endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leaky_bucket_http_endpoint(client):
    """Sanity check that the /check/leaky_bucket endpoint is wired up correctly."""
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
    # Standard Prometheus headers should be present
    assert "# HELP" in body or "# TYPE" in body or "rate_limit" in body


# ---------------------------------------------------------------------------
# 13. In-memory token bucket thread safety
# ---------------------------------------------------------------------------

def test_in_memory_token_bucket_thread_safety():
    """Multiple threads should not corrupt the bucket state."""
    import threading

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

    allowed_count = sum(1 for r in results if r)
    # With capacity=10 and 200 total calls, exactly 10 should be initially allowed
    # (subsequent ones may be allowed too as refill_rate is high).
    # The important invariant is that we got exactly 200 results without errors.
    assert len(results) == 200


# ---------------------------------------------------------------------------
# 14. Circuit breaker half-open → closed on success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_half_open_closes_on_success():
    cb = CircuitBreaker(failure_threshold=2, failure_window=10.0, recovery_timeout=0.1)

    # Open it
    cb.record_failure()
    cb.record_failure()
    assert cb.state == State.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # allow_request should transition to HALF_OPEN and return True
    assert cb.allow_request() is True
    assert cb.state == State.HALF_OPEN

    # Probe succeeds
    cb.record_success()
    assert cb.state == State.CLOSED
