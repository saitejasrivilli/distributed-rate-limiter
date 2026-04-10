# Distributed Rate Limiter

[![Live API](https://img.shields.io/badge/Live%20API-Swagger%20Docs-blue)](https://YOUR-APP.onrender.com/docs)
[![Redis](https://img.shields.io/badge/Redis-Upstash-green)](https://upstash.com)
[![Deploy](https://img.shields.io/badge/Deploy-Render-purple)](https://render.com)

A **production-hardened** distributed rate limiter with three algorithms, atomic Lua scripts, full security hardening, and a live interactive Swagger UI.

## Live Demo

👉 **[YOUR-APP.onrender.com/docs](https://YOUR-APP.onrender.com/docs)**

---

## Algorithms

| Algorithm | Redis Structure | Best For |
|---|---|---|
| **Sliding Window** | Sorted Set (ZSET) | Smoothest — no boundary burst |
| **Fixed Window** | String (INCR) | Lowest latency |
| **Token Bucket** | Hash (HMSET) | Burst tolerance |

## Architecture

```
Client Request
      │
      ▼
FastAPI (Render)
  ├─ RequestID middleware    ← every request gets a UUID
  ├─ SecurityHeaders         ← X-Content-Type-Options, X-Frame-Options, etc.
  ├─ RequestSizeLimit        ← 64 KB cap, blocks body-bomb attacks
  ├─ Input validation        ← client_id regex, server-side limit caps
  └─ Self-protection         ← the API rate-limits itself (500 req/10s)
      │
      ▼
Lua Script (atomic, single round-trip)
      │
      ▼
Upstash Redis (shared across all instances)
  ├─ rl:sw:{client_id}        ← ZSET for sliding window
  ├─ rl:fw:{client_id}:{bucket} ← STRING for fixed window
  └─ rl:tb:{client_id}        ← HASH for token bucket
```

## Security Hardening

| Attack | Defence |
|---|---|
| Redis key injection (`user:../admin`) | Strict `^[a-zA-Z0-9_\-.@:]+$` regex on `client_id` |
| Bypass own limit (`limit=999999`) | Server-side caps: limit ≤ 10,000; window ≤ 86,400s |
| DoS the rate limiter itself | Self-protection: 500 req/10s cap on all check endpoints |
| Flood `/simulate` | Separate limit: 20 calls/min on simulate endpoint |
| Body-bomb on POST | 64 KB request size limit middleware |
| Stack trace leaks | Global exception handler; structured JSON logs only |
| Unauthenticated reset | `/reset` requires `X-Admin-Key` header (403 otherwise) |
| ZSET member collision | Atomic sequence counter (replaces unseeded `math.random`) |
| Redis connection exhaustion | Connection pool (max 20), connect/socket timeouts |
| KEYS scan blocking Redis | Explicit key patterns in reset; no `KEYS *` ever |
| Missing RFC headers | `Retry-After` on 429, `X-RateLimit-Remaining` on all responses |
| Clickjacking / MIME sniff | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` |

## API Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Health check |
| `GET` | `/check/sliding_window` | — | Check sliding window limit |
| `GET` | `/check/fixed_window` | — | Check fixed window limit |
| `GET` | `/check/token_bucket` | — | Check token bucket limit |
| `POST` | `/simulate` | — | Burst simulator (≤100 requests) |
| `DELETE` | `/reset/{client_id}` | `X-Admin-Key` | Reset client counters |

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

## Deploy (Free, ~10 minutes)

### 1. Get Upstash Redis
1. [console.upstash.com](https://console.upstash.com) → create Redis DB
2. Copy the `rediss://` connection URL

### 2. Deploy to Render
1. Fork this repo on GitHub
2. [render.com](https://render.com) → New Web Service → connect your fork
3. Set env vars:
   - `UPSTASH_REDIS_URL` = your Upstash URL
   - `ADMIN_API_KEY` = any secret string (e.g. `openssl rand -hex 32`)
4. Build: `pip install -r requirements.txt`
5. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### 3. Keep alive (prevent Render cold starts)
Add a free [UptimeRobot](https://uptimerobot.com) monitor hitting `YOUR-APP.onrender.com/` every 5 minutes.

### Run locally
```bash
git clone https://github.com/YOUR_USERNAME/distributed-rate-limiter
cd distributed-rate-limiter
cp .env.example .env          # fill in UPSTASH_REDIS_URL + ADMIN_API_KEY
pip install -r requirements.txt
uvicorn app.main:app --reload
# → http://localhost:8000/docs
```

## Simulate a burst

```bash
curl -X POST https://YOUR-APP.onrender.com/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "algorithm": "sliding_window",
    "client_id": "demo",
    "request_count": 15,
    "limit": 10,
    "window_seconds": 60
  }'
```

```json
{
  "algorithm": "sliding_window",
  "total_requests": 15,
  "allowed": 10,
  "blocked": 5,
  "block_rate_pct": 33.3
}
```

## Interview Questions Answered in This Code

**Q: Why Lua scripts instead of MULTI/EXEC?**
Lua executes server-side in a single round-trip and is truly atomic. MULTI/EXEC uses optimistic locking — it can still fail under high contention and requires client-side retry logic.

**Q: How does this scale across multiple instances?**
All instances share Upstash Redis. The Lua atomicity guarantees correctness at any scale — no instance can read stale state.

**Q: Sliding window vs token bucket memory tradeoff?**
Sliding window uses O(requests_in_window) memory per client — one ZSET entry per request. Token bucket uses O(1) per client regardless of traffic volume.

**Q: How do you handle Redis failure?**
Fail open — requests are allowed through when Redis is unavailable. This is logged as an error. For fraud/security contexts you'd invert this (fail closed) and add a circuit breaker.

**Q: How do you prevent the KEYS scan from blocking Redis?**
The `reset()` method never uses `KEYS *`. It instead constructs explicit key names for all possible time buckets within a safe range, then pipelines the deletes in a single round-trip.

## Tech Stack
- **FastAPI** — async, auto-generates Swagger at `/docs`
- **Upstash Redis** — serverless, free tier, persistent
- **Lua scripts** — atomic read-modify-write
- **Render** — free hosting, deploy from GitHub
