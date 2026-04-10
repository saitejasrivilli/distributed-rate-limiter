# Distributed Rate Limiter

[![Live Dashboard](https://img.shields.io/badge/Live-Dashboard-00ff88?style=flat&labelColor=0a0a0a)](https://distributed-rate-limiter-orm2.onrender.com)
[![Swagger UI](https://img.shields.io/badge/API-Swagger_Docs-4488ff?style=flat&labelColor=0a0a0a)](https://distributed-rate-limiter-orm2.onrender.com/docs)
[![Redis](https://img.shields.io/badge/Redis-Upstash-00cc6a?style=flat&labelColor=0a0a0a)](https://upstash.com)
[![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat&labelColor=0a0a0a)](https://python.org)
[![Deploy](https://img.shields.io/badge/Deployed-Render-purple?style=flat&labelColor=0a0a0a)](https://render.com)

Production-grade distributed rate limiter  three algorithms, atomic Redis Lua scripts, 12 security hardening measures, and a live interactive dashboard.

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
  ├── RequestSizeLimit middleware 64 KB body cap  blocks body-bomb attacks
  ├── Input validation            client_id regex, server-side limit caps
  └── Self-protection             rate-limits its own /check endpoints (500 req/10s)
         │
         ▼
    Lua Script (atomic  single Redis round-trip, no race conditions)
         │
         ▼
    Upstash Redis (shared state across all app instances)
      ├── rl:sw:{client_id}           ZSET   sliding window
      ├── rl:sw:seq:{client_id}       STRING  atomic sequence counter
      ├── rl:fw:{client_id}:{bucket}  STRING  fixed window
      └── rl:tb:{client_id}           HASH    token bucket
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
Stores `{tokens, last_refill}` in a Redis Hash. Virtual refill on every read  no background worker needed.

```
elapsed = (now - last_refill) / 1000.0
tokens = min(capacity, tokens + elapsed × refill_rate)
if tokens >= 1:
    tokens -= 1
    HMSET key tokens {tokens} last_refill {now}
    return allowed
```

**Tradeoff:** O(1) memory per client regardless of traffic volume. Allows controlled bursting  full bucket = burst allowed.



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
| `GET` | `/health` |  | Redis health check (JSON) |
| `GET` | `/check/sliding_window` |  | Check sliding window limit |
| `GET` | `/check/fixed_window` |  | Check fixed window limit |
| `GET` | `/check/token_bucket` |  | Check token bucket limit |
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
| Hosting | Render (free tier) |
| Language | Python 3.12 |
| Validation | Pydantic v2 |
