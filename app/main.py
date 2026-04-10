import uuid
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Security, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware

from app.limiter import RateLimiter
from app.models import (
    RateLimitResponse, AlgorithmType,
    SimulateRequest, SimulateResponse, SimulateResult,
    StatusResponse, ErrorResponse,
)
from app.validators import (
    validate_client_id, validate_limit,
    validate_window, validate_capacity, validate_refill_rate,
)
from app.config import (
    SELF_PROTECT_LIMIT, SELF_PROTECT_WINDOW,
    SIMULATE_PROTECT_LIMIT, SIMULATE_PROTECT_WINDOW,
    get_admin_key,
)
from app.logging_config import logger

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
limiter: RateLimiter = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global limiter
    limiter = RateLimiter()
    try:
        await limiter.connect()
        key = get_admin_key()
        from app.config import ADMIN_API_KEY
        if not ADMIN_API_KEY:
            logger.warning(
                "ADMIN_API_KEY not set — generated ephemeral key for this session",
                extra={"generated_admin_key": key}
            )
    except Exception as exc:
        logger.error("Failed to connect to Redis at startup", extra={"exc_msg": str(exc)})
        raise
    yield
    await limiter.close()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Distributed Rate Limiter",
    description="""
## Distributed Rate Limiter — Live Demo

A production-grade rate limiter backed by **Upstash Redis**, supporting three algorithms:

| Algorithm | Best For |
|---|---|
| **Sliding Window** | Smoothest limiting, no burst at window edge |
| **Fixed Window** | Simple, lowest latency |
| **Token Bucket** | Allows controlled bursts |

### How to test
1. Pick an algorithm under **Rate Limit — Check**
2. Call `/check/{algorithm}` with a `client_id`
3. Watch `remaining` count down; see `retry_after` when blocked
4. Use **POST /simulate** to fire N requests and see the full breakdown

### Security
- `/reset/{client_id}` requires `X-Admin-Key` header
- All inputs are validated server-side; caller-supplied `limit` values are capped
- The API rate-limits itself against flooding
    """,
    version="2.0.0",
    lifespan=lifespan,
    # Never expose internal errors to clients
    responses={
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        400: {"model": ErrorResponse, "description": "Invalid input"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    }
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# 1. Request ID — every request gets a unique ID for tracing
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

# 2. Security headers — added to every response
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

# 3. Request size limit — prevent body-bomb attacks on /simulate
class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    MAX_BODY = 64 * 1024  # 64 KB

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > self.MAX_BODY:
                return JSONResponse(
                    status_code=413,
                    content={"error": "request_too_large", "detail": "Request body exceeds 64 KB"}
                )
        return await call_next(request)

# 4. Global error handler — never leak stack traces to clients
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(
        "Unhandled exception",
        exc_info=exc,
        extra={"path": request.url.path, "request_id": request_id}
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": "An unexpected error occurred. Please try again.",
            "request_id": request_id,
        }
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", "unknown")
    content = {"error": "http_error", "detail": exc.detail, "request_id": request_id}

    headers = {}
    # RFC 6585: include Retry-After header on 429
    if exc.status_code == 429 and isinstance(exc.detail, dict):
        retry = exc.detail.get("retry_after_seconds")
        if retry:
            headers["Retry-After"] = str(retry)

    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # open for recruiter demo; restrict to your domain in prod
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Admin-Key"],
    expose_headers=["X-Request-ID", "Retry-After", "X-RateLimit-Remaining"],
)

# ---------------------------------------------------------------------------
# Auth dependency — admin endpoints only
# ---------------------------------------------------------------------------
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)

async def require_admin(api_key: str = Security(admin_key_header)):
    if not api_key or api_key != get_admin_key():
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing X-Admin-Key header"
        )


# ---------------------------------------------------------------------------
# Helper — add rate limit headers to response
# ---------------------------------------------------------------------------
def rl_headers(result: RateLimitResponse) -> dict:
    h = {"X-RateLimit-Limit": str(result.limit),
         "X-RateLimit-Remaining": str(result.remaining)}
    if result.retry_after is not None:
        h["Retry-After"] = str(result.retry_after)
    return h


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_model=StatusResponse, tags=["Health"])
async def root():
    """Health check — confirms Redis connection is alive."""
    redis_ok = await limiter.ping()
    return StatusResponse(
        status="ok" if redis_ok else "degraded",
        redis_connected=redis_ok,
        algorithms_available=["sliding_window", "fixed_window", "token_bucket"],
        message="Rate limiter is running. Visit /docs to test interactively."
    )


@app.get(
    "/check/sliding_window",
    response_model=RateLimitResponse,
    tags=["Rate Limit — Check"],
    summary="Sliding Window counter",
)
async def check_sliding_window(
    request: Request,
    client_id: str = "demo_user",
    limit: int = 10,
    window_seconds: int = 60,
):
    """
    **Sliding Window** — rolling time window, no boundary burst.

    Uses a Redis sorted set scored by timestamp. Entries outside the window
    are pruned atomically via a single Lua script (no race conditions).

    Try it: call 10 times with the same `client_id` to get a 429.
    """
    cid = validate_client_id(client_id)
    lim = validate_limit(limit)
    win = validate_window(window_seconds)

    # Self-protect the rate limiter against being flooded via the demo API
    allowed = await limiter.self_protect("check", SELF_PROTECT_LIMIT, SELF_PROTECT_WINDOW)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "service_overloaded",
                "detail": "Too many requests to the demo API itself. Try again shortly.",
                "retry_after_seconds": SELF_PROTECT_WINDOW,
            }
        )

    result = await limiter.sliding_window(cid, lim, win)

    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "algorithm": "sliding_window",
                "retry_after_seconds": result.retry_after,
                "limit": result.limit,
                "window_seconds": win,
                "request_id": request.state.request_id,
            }
        )

    from fastapi.responses import JSONResponse as JR
    return JR(content=result.model_dump(), headers=rl_headers(result))


@app.get(
    "/check/fixed_window",
    response_model=RateLimitResponse,
    tags=["Rate Limit — Check"],
    summary="Fixed Window counter",
)
async def check_fixed_window(
    request: Request,
    client_id: str = "demo_user",
    limit: int = 10,
    window_seconds: int = 60,
):
    """
    **Fixed Window** — simplest algorithm, lowest latency.

    Known tradeoff: burst at window boundary can allow 2× limit.
    Try it: call 10 times to hit the limit, then wait for the window to reset.
    """
    cid = validate_client_id(client_id)
    lim = validate_limit(limit)
    win = validate_window(window_seconds)

    allowed = await limiter.self_protect("check", SELF_PROTECT_LIMIT, SELF_PROTECT_WINDOW)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "service_overloaded",
                "retry_after_seconds": SELF_PROTECT_WINDOW,
            }
        )

    result = await limiter.fixed_window(cid, lim, win)

    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "algorithm": "fixed_window",
                "retry_after_seconds": result.retry_after,
                "limit": result.limit,
                "window_seconds": win,
                "request_id": request.state.request_id,
            }
        )

    from fastapi.responses import JSONResponse as JR
    return JR(content=result.model_dump(), headers=rl_headers(result))


@app.get(
    "/check/token_bucket",
    response_model=RateLimitResponse,
    tags=["Rate Limit — Check"],
    summary="Token Bucket (allows bursting)",
)
async def check_token_bucket(
    request: Request,
    client_id: str = "demo_user",
    capacity: int = 10,
    refill_rate: float = 1.0,
):
    """
    **Token Bucket** — allows controlled bursts.

    Tokens refill continuously at `refill_rate`/sec. Full bucket = burst allowed.
    Try it: call rapidly to drain the bucket, then watch tokens refill.
    """
    cid = validate_client_id(client_id)
    cap = validate_capacity(capacity)
    rate = validate_refill_rate(refill_rate)

    allowed = await limiter.self_protect("check", SELF_PROTECT_LIMIT, SELF_PROTECT_WINDOW)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "service_overloaded",
                "retry_after_seconds": SELF_PROTECT_WINDOW,
            }
        )

    result = await limiter.token_bucket(cid, cap, rate)

    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "algorithm": "token_bucket",
                "retry_after_seconds": result.retry_after,
                "tokens_remaining": result.remaining,
                "capacity": cap,
                "request_id": request.state.request_id,
            }
        )

    from fastapi.responses import JSONResponse as JR
    return JR(content=result.model_dump(), headers=rl_headers(result))


@app.post(
    "/simulate",
    response_model=SimulateResponse,
    tags=["Demo"],
    summary="Fire N requests at once — see allowed vs blocked",
)
async def simulate(request: Request, req: SimulateRequest):
    """
    **Burst simulator** — fires `request_count` requests and shows the full breakdown.

    Capped at 100 requests per call. The `/simulate` endpoint itself is
    rate-limited (20 calls/min) to prevent Redis flooding via the demo UI.
    """
    allowed_svc = await limiter.self_protect(
        "simulate", SIMULATE_PROTECT_LIMIT, SIMULATE_PROTECT_WINDOW
    )
    if not allowed_svc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "simulate_rate_exceeded",
                "detail": f"Simulate endpoint is limited to {SIMULATE_PROTECT_LIMIT} calls per {SIMULATE_PROTECT_WINDOW}s",
                "retry_after_seconds": SIMULATE_PROTECT_WINDOW,
            }
        )

    # Re-validate (Pydantic already validated, but be explicit)
    cid = validate_client_id(req.client_id)

    results: list[SimulateResult] = []
    allowed_count = 0
    blocked_count = 0

    for i in range(req.request_count):
        if req.algorithm == AlgorithmType.sliding_window:
            r = await limiter.sliding_window(cid, req.limit, req.window_seconds)
        elif req.algorithm == AlgorithmType.fixed_window:
            r = await limiter.fixed_window(cid, req.limit, req.window_seconds)
        else:
            r = await limiter.token_bucket(cid, req.capacity, req.refill_rate)

        if r.allowed:
            allowed_count += 1
        else:
            blocked_count += 1

        results.append(SimulateResult(
            request_number=i + 1,
            allowed=r.allowed,
            remaining=r.remaining,
            retry_after=r.retry_after,
        ))

    return SimulateResponse(
        algorithm=req.algorithm,
        client_id=cid,
        total_requests=req.request_count,
        allowed=allowed_count,
        blocked=blocked_count,
        block_rate_pct=round(blocked_count / req.request_count * 100, 1),
        results=results,
    )


@app.delete(
    "/reset/{client_id}",
    tags=["Admin"],
    summary="Reset a client's rate limit counters (requires X-Admin-Key)",
    dependencies=[Depends(require_admin)],
)
async def reset_client(request: Request, client_id: str):
    """
    Clears all rate limit state for a `client_id`.

    **Requires** `X-Admin-Key` header — prevents anyone from resetting other users' limits.

    In the Swagger UI, click **Authorize** (top right) and enter your admin key.
    """
    cid = validate_client_id(client_id)
    deleted = await limiter.reset(cid)
    logger.info("Client reset", extra={"client_id": cid, "keys_deleted": deleted})
    return {"client_id": cid, "keys_deleted": deleted, "status": "reset"}
