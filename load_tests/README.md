# Load Tests — Distributed Rate Limiter

Locust-based load tests targeting all four rate-limit algorithms at 500 concurrent users.

## Prerequisites

```bash
pip install locust
```

## Quick start (headless, 120 s run)

```bash
# Start the app first (separate terminal)
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Run the load test
locust -f load_tests/locustfile.py \
       --host http://localhost:8000 \
       --users 500 \
       --spawn-rate 10 \
       --run-time 120s \
       --headless \
       --html load_tests/report.html
```

The `--html` flag writes a full interactive HTML report to `load_tests/report.html`.

## Interactive mode (Locust web UI)

```bash
locust -f load_tests/locustfile.py --host http://localhost:8000
# Open http://localhost:8089 in a browser
# Set Users = 500, Spawn rate = 10, then Start
```

## Against the live Render deployment

```bash
locust -f load_tests/locustfile.py \
       --host https://distributed-rate-limiter-orm2.onrender.com \
       --users 50 \
       --spawn-rate 5 \
       --run-time 60s \
       --headless
```

Note: reduce users when targeting the free Render tier to avoid overwhelming the
shared Upstash Redis instance.

## Test structure

| User class | Behaviour | Weight |
|---|---|---|
| `RateLimiterUser` | Mixed traffic across all 4 algorithms | Primary |
| `BurstUser` | Floods sliding_window to stress 429 path | 1 |
| `SteadyUser` | Constant-throughput token + leaky bucket | 1 |

### Task weights within `MixedTasks`

| Endpoint | Weight | Reason |
|---|---|---|
| `/check/sliding_window` | 4 | Most common in production |
| `/check/fixed_window` | 3 | Low-latency O(1) path |
| `/check/token_bucket` | 2 | Burst scenarios |
| `/check/leaky_bucket` | 2 | Constant-rate enforcement |
| `/health` | 1 | Monitoring probe |

HTTP 429 responses are marked as **success** in Locust — a rejection from the
rate limiter is correct behaviour, not an error.

## Expected results

Measured on a local Redis instance (Redis 7, same machine):

| Metric | Target | Typical observed |
|---|---|---|
| Median (p50) | < 5 ms | 2–4 ms |
| p95 | < 8 ms | 5–7 ms |
| p99 | < 10 ms | 7–10 ms |
| Throughput (RPS) | > 1 000 | 2 000–5 000 |
| Error rate (5xx) | 0 % | 0 % |
| 429 rate | ~20–40 % | Depends on limit params |

Against the live Upstash endpoint, add ~10–20 ms of network RTT to all figures.

## CI integration

The CI pipeline (`ci.yml`) runs the unit/integration test suite but does **not**
run Locust automatically — load tests require a running server and are intended
for manual pre-release validation or dedicated perf-testing infrastructure.

To add a smoke locust run to CI (optional):

```yaml
- name: Smoke load test (30 s)
  run: |
    pip install locust
    uvicorn app.main:app --host 0.0.0.0 --port 8000 &
    sleep 3
    locust -f load_tests/locustfile.py --host http://localhost:8000 \
           --users 50 --spawn-rate 10 --run-time 30s --headless
```
