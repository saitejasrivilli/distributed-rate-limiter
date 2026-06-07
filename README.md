# Distributed Rate Limiter

[![CI](https://github.com/saitejasrivilli/distributed-rate-limiter/actions/workflows/ci.yml/badge.svg)](https://github.com/saitejasrivilli/distributed-rate-limiter/actions/workflows/ci.yml)
[![Live Dashboard](https://img.shields.io/badge/Live-Dashboard-00ff88?style=flat&labelColor=0a0a0a)](https://distributed-rate-limiter-orm2.onrender.com)
[![Swagger UI](https://img.shields.io/badge/API-Swagger_Docs-4488ff?style=flat&labelColor=0a0a0a)](https://distributed-rate-limiter-orm2.onrender.com/docs)
[![Redis](https://img.shields.io/badge/Redis-Upstash-00cc6a?style=flat&labelColor=0a0a0a)](https://upstash.com)
[![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat&labelColor=0a0a0a)](https://python.org)
[![Deploy](https://img.shields.io/badge/Deployed-Render-purple?style=flat&labelColor=0a0a0a)](https://render.com)

Production-grade distributed rate limiter — four algorithms, atomic Redis Lua scripts, circuit breaker with in-memory fallback, Prometheus metrics, and a live interactive dashboard.

## Live Demo

**Dashboard →** https://distributed-rate-limiter-orm2.onrender.com

**API (Swagger) →** https://distributed-rate-limiter-orm2.onrender.com/docs

> Open the dashboard, enable auto-fire, and watch the pressure gauge fill and the retry countdown tick in real time.



## What this demonstrates

This project is built to answer the system design questions that come up at every backend interview:

- **Rate limiting at scale**  how do you enforce limits across multiple app instances without race conditions?
- **Algorithm tradeoffs**  when do you use sliding window vs fixed window vs token bucket?
- **Redis atomicity**  why Lua scripts over MULTI/EXEC?
- **Production hardening**  what attacks does a rate limiter itself need to defend against?



## Architecture

```
Client
  │
  ▼
FastAPI on Render
  ├── RequestID middleware        UUID on every request for tracing
  ├── SecurityHeaders middleware  X-Frame-Options, X-Content-Type-Options, etc.
  ├── RequestSizeLimit middleware 64 KB body cap — blocks body-bomb attacks
  ├── Input validation            client_id regex, server-side limit caps
  └── Self-protection             rate-limits its own /check endpoints (500 req/10s)
         │
         ▼
    Circuit Breaker (CLOSED → OPEN after 5 failures / 10s → HALF_OPEN after 30s)
         │                │
         │           OPEN: fallback to in-memory token bucket (threading.Lock)
         ▼
    Lua Script (atomic — single Redis round-trip, no race conditions)
         │
         ▼
    Upstash Redis (shared state across all app instances)
      ├── rl:sw:{client_id}           ZSET    sliding window
      ├── rl:sw:seq:{client_id}       STRING  atomic sequence counter
      ├── rl:fw:{client_id}:{bucket}  STRING  fixed window
      ├── rl:tb:{client_id}           HASH    token bucket
      └── rl:lb:{client_id}           HASH    leaky bucket

Prometheus → GET /metrics  (rate_limit_hits_total, rate_limit_rejections_total,
                             redis_operation_duration_seconds)
```



## Algorithms

### Sliding Window  `GET /check/sliding_window`
Stores each request as a scored member in a Redis sorted set (score = timestamp ms). On every request, a Lua script atomically prunes expired entries, counts remaining, and inserts if under limit.

```
ZREMRANGEBYSCORE key -inf (now - window_ms)
count = ZCARD key
if count < limit:
    ZADD key now "{now}-{seq}"   -- seq from atomic INCR, no collision
    PEXPIRE key window_ms + 1000
    return {allowed, remaining, 0}
else:
    retry = oldest_score + window_ms - now
    return {blocked, 0, retry_secs}
```

**Tradeoff:** Memory is O(requests_in_window) per client. Best general-purpose choice  no boundary burst.

### Fixed Window  `GET /check/fixed_window`
Counts requests in fixed time buckets. Key includes the bucket number so keys auto-rotate without any cleanup job.

```
bucket = floor(now_ms / window_ms)
key = "rl:fw:{client}:{bucket}"
count = INCR key
if count == 1: EXPIRE key window_sec + 1
if count <= limit: allow
```

**Tradeoff:** O(1) memory, one Redis call  lowest latency. Known issue: 2× burst possible at window boundary.

### Token Bucket  `GET /check/token_bucket`
Stores `{tokens, last_refill}` in a Redis Hash. Virtual refill on every read — no background worker needed.

```
elapsed = (now - last_refill) / 1000.0
tokens = min(capacity, tokens + elapsed × refill_rate)
if tokens >= 1:
    tokens -= 1
    HMSET key tokens {tokens} last_refill {now}
    return allowed
```

**Tradeoff:** O(1) memory per client regardless of traffic volume. Allows controlled bursting — full bucket = burst allowed.

### Leaky Bucket  `GET /check/leaky_bucket`
Stores `{queue_size, last_leak_ms}` in a Redis Hash. Drains at a constant `leak_rate` items/sec. Accepts the request if the queue is not full; rejects if full.

```
elapsed = (now_ms - last_leak_ms) / 1000.0
leaked  = floor(elapsed × leak_rate)
queue   = max(0, queue - leaked)
if queue < capacity:
    queue += 1
    HMSET key queue_size {queue} last_leak_ms {adjusted_ts}
    return {allowed, capacity - queue, 0}
else:
    return {rejected, 0, ceil(1 / leak_rate)}
```

**Tradeoff:** O(1) memory. Unlike token bucket, bursts are **queued not absorbed** — the output rate is strictly constant. Best for smoothing bursty upstream traffic into a steady downstream rate.



## Security Hardening

14 production security measures implemented:

| Attack vector | Defence |
|||
| Redis key injection (`user:../admin`) | `^[a-zA-Z0-9_\-.@:]+$` regex, 128-char max |
| Bypass own limit (`limit=999999`) | Server-side caps  caller input never trusted |
| DoS the rate limiter itself | Self-protection Lua script: 500 req/10s global cap |
| Flood `/simulate` | Separate limit: 20 calls/60s on simulate |
| Body-bomb on POST | 64 KB request size middleware |
| Stack trace leaks | Global exception handler  sanitised errors only |
| Unauthenticated `/reset` | `X-Admin-Key` required, 403 otherwise |
| ZSET member collision at concurrency | Atomic `INCR` sequence counter  no `math.random` |
| `KEYS *` blocking Redis | Explicit key construction, pipelined deletes  no scan |
| Missing RFC 6585 headers | `Retry-After` on every 429 |
| No request tracing | `X-Request-ID` UUID on every request and error |
| Redis connection exhaustion | Pool (max 20), connect timeout 5s, socket timeout 3s |
| Clickjacking / MIME sniff | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` |
| Near-zero refill_rate in Lua | `MIN_REFILL_RATE = 0.01` enforced server-side |



## API Reference

| Method | Endpoint | Auth | Description |
|||||
| `GET` | `/` |  | Dashboard UI |
| `GET` | `/health` |  | Redis health + circuit breaker state (JSON) |
| `GET` | `/metrics` |  | Prometheus metrics |
| `GET` | `/check/sliding_window` |  | Check sliding window limit |
| `GET` | `/check/fixed_window` |  | Check fixed window limit |
| `GET` | `/check/token_bucket` |  | Check token bucket limit |
| `GET` | `/check/leaky_bucket` |  | Check leaky bucket limit |
| `POST` | `/simulate` |  | Burst simulator (≤100 requests) |
| `DELETE` | `/reset/{client_id}` | `X-Admin-Key` | Reset all counters for a client |

### 429 Response (RFC 6585 compliant)
```
HTTP/1.1 429 Too Many Requests
Retry-After: 45
X-RateLimit-Remaining: 0
X-Request-ID: 3f7a2b1c-...

{
  "error": "rate_limit_exceeded",
  "algorithm": "sliding_window",
  "retry_after_seconds": 45,
  "limit": 10,
  "request_id": "3f7a2b1c-..."
}
```



## Run locally

```bash
git clone https://github.com/saitejasrivilli/distributed-rate-limiter
cd distributed-rate-limiter

python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export UPSTASH_REDIS_URL="rediss://default:YOUR_PASSWORD@YOUR_HOST.upstash.io:6379?ssl_cert_reqs=none"
export ADMIN_API_KEY="your_secret_key"

uvicorn app.main:app --reload
# Dashboard → http://localhost:8000
# Swagger   → http://localhost:8000/docs
```



## Deploy your own (free, ~10 min)

**1. Upstash Redis**  [console.upstash.com](https://console.upstash.com) → Create Database → copy `rediss://` URL

**2. Render**  [render.com](https://render.com) → New Web Service → connect this repo

| Setting | Value |
|||
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| `UPSTASH_REDIS_URL` | your `rediss://` URL |
| `ADMIN_API_KEY` | any secret string |

**3. Keep alive**  [UptimeRobot](https://uptimerobot.com) → HTTP monitor → ping `/health` every 5 min (prevents Render free tier sleep)

**4. Pin Python version**  `.python-version` file in repo pins to 3.12.13 (required  pydantic-core has no wheels for 3.14)



## Key interview questions this project answers

**Why Lua scripts instead of MULTI/EXEC?**
Lua executes server-side atomically in a single round-trip. MULTI/EXEC uses optimistic locking and can fail under contention  the client must retry. Lua failures never leave partial state.

**How does this scale to multiple app instances?**
All instances point to the same Upstash Redis. Lua atomicity ensures correctness regardless of how many instances are running  no instance can read stale state between a check and an update.

**Sliding window vs token bucket  when do you pick each?**
Sliding window: smooth limiting, O(log N) per request, memory proportional to traffic volume. Token bucket: O(1) memory per client regardless of volume, allows bursting. For high-volume clients where memory matters, token bucket wins.

**How do you handle Redis going down?**
Fail open  requests are allowed through and the error is logged. For fraud prevention or security-critical contexts you'd fail closed and add a circuit breaker with local in-memory fallback.

**Why no `KEYS *` in the reset endpoint?**
`KEYS *` blocks the entire Redis keyspace for the duration of the scan  catastrophic in production. The reset method instead constructs explicit key names for all possible time buckets and pipelines the deletes in a single round-trip.



## Tech stack

| Layer | Technology |
|||
| API framework | FastAPI (async Python 3.12) |
| Database | Upstash Redis (serverless, free tier) |
| Atomicity | Redis Lua scripts |
| Observability | Prometheus + prometheus-client |
| Reliability | Circuit breaker + in-memory fallback |
| Hosting | Render (free tier) |
| Language | Python 3.12 |
| Validation | Pydantic v2 |



## Observability

### Prometheus metrics — `GET /metrics`

| Metric | Type | Labels |
||||
| `rate_limit_hits_total` | Counter | `algorithm` |
| `rate_limit_rejections_total` | Counter | `algorithm` |
| `redis_operation_duration_seconds` | Histogram | `algorithm` |

All four algorithms emit these metrics on every request. The histogram uses
sub-millisecond buckets (1ms, 2ms, 5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s)
suitable for SLO alerting.

Scrape config example:

```yaml
scrape_configs:
  - job_name: rate_limiter
    static_configs:
      - targets: ["localhost:8000"]
    metrics_path: /metrics
    scrape_interval: 15s
```

### Health endpoint — `GET /health`

```json
{
  "status": "ok",
  "redis_connected": true,
  "algorithms_available": ["sliding_window", "fixed_window", "token_bucket", "leaky_bucket"],
  "circuit_breaker_state": "closed",
  "message": "Rate limiter is running."
}
```

`circuit_breaker_state` is one of `closed | open | half_open`.



## Reliability — Circuit Breaker

The circuit breaker (`app/circuit_breaker.py`) protects the service when Redis
is unavailable:

```
                  ┌──────────────────────────────────────────────┐
                  │           5 failures / 10s                    │
    CLOSED ──────────────────────────────────────────► OPEN       │
       ▲          │                                      │        │
       │          │            30s elapsed               │        │
       │          │◄─────────────────────────── HALF_OPEN         │
       │                                          │               │
       └──── probe succeeds ──────────────────────┘               │
                        probe fails ──────────────────────────────►
```

When OPEN, all Redis calls are short-circuited and requests are served by a
**per-instance in-memory token bucket** (thread-safe via `threading.Lock`).
This maintains availability at the cost of consistency — appropriate for
rate-limiting use cases where brief overcounting is acceptable.

The in-memory fallback is intentionally **not distributed** — under Redis outage,
each instance enforces limits independently. Add a secondary Redis or local
SQLite if strict enforcement during outage is required.



## Lua Script Latency (measured)

Benchmarked against local Redis 5.0, 2 000 iterations each, loopback socket.

| Algorithm | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| sliding_window | 0.080 ms | 0.079 ms | 0.084 ms | 0.100 ms | 0.134 ms |
| fixed_window | 0.079 ms | 0.087 ms | 0.091 ms | 0.134 ms | 0.225 ms |
| token_bucket | 0.072 ms | 0.071 ms | 0.074 ms | 0.092 ms | 0.132 ms |
| leaky_bucket | 0.071 ms | 0.071 ms | 0.073 ms | 0.091 ms | 0.133 ms |

All four algorithms execute atomically in **< 0.15 ms at p99** on loopback.  
Against Upstash add ~10–20 ms network RTT (one round-trip per request).

Reproduce: `python bench_lua_latency.py` (requires local Redis on :6379)

---

## Load Testing

Uses [Locust](https://locust.io) — see `load_tests/` for full details.

```bash
pip install locust
locust -f load_tests/locustfile.py --host http://localhost:8000 \
       --users 500 --spawn-rate 10 --run-time 120s --headless \
       --html load_tests/report.html
```

### Expected results at 500 concurrent users (local Redis 7)

| Metric | Target | Typical |
||||
| p50 latency | < 5 ms | 2–4 ms |
| p95 latency | < 8 ms | 5–7 ms |
| p99 latency | < 10 ms | 7–10 ms |
| Throughput | > 1 000 RPS | 2 000–5 000 RPS |
| Error rate (5xx) | 0 % | 0 % |

429 responses are expected and counted as success — the rate limiter is working
as designed. Against the live Upstash endpoint add ~10–20 ms network RTT.



## Running Tests

```bash
pip install pytest pytest-asyncio httpx fakeredis
pytest tests/ -v
```

Tests use `fakeredis` for in-process Redis emulation — no real Redis or network
required. The CI pipeline (`ci.yml`) runs these tests against a real Redis 7
Docker service on every push and pull request.
