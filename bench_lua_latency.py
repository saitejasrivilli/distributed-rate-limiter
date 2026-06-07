"""
Benchmark: measure actual p50/p95/p99 latency of each Lua script
against local Redis. Run with: python bench_lua_latency.py
"""
import time
import statistics
import redis
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.limiter import (
    SLIDING_WINDOW_SCRIPT,
    FIXED_WINDOW_SCRIPT,
    TOKEN_BUCKET_SCRIPT,
    LEAKY_BUCKET_SCRIPT,
)

ITERS = 2000
WARMUP = 200

r = redis.Redis(host="localhost", port=6379, decode_responses=True, protocol=2)
r.ping()
r.flushdb()

scripts = {
    "sliding_window": r.register_script(SLIDING_WINDOW_SCRIPT),
    "fixed_window":   r.register_script(FIXED_WINDOW_SCRIPT),
    "token_bucket":   r.register_script(TOKEN_BUCKET_SCRIPT),
    "leaky_bucket":   r.register_script(LEAKY_BUCKET_SCRIPT),
}

def bench(name, script, keys_fn, args_fn):
    # warmup
    for i in range(WARMUP):
        now_ms = int(time.time() * 1000)
        script(keys=keys_fn(i, "wm"), args=args_fn(now_ms))

    latencies = []
    for i in range(ITERS):
        now_ms = int(time.time() * 1000)
        keys = keys_fn(i, "bm")
        args = args_fn(now_ms)
        t0 = time.perf_counter()
        script(keys=keys, args=args)
        latencies.append((time.perf_counter() - t0) * 1000)  # ms

    latencies.sort()
    p50  = latencies[int(ITERS * 0.50)]
    p95  = latencies[int(ITERS * 0.95)]
    p99  = latencies[int(ITERS * 0.99)]
    pmax = latencies[-1]
    mean = statistics.mean(latencies)
    print(f"{name:20s}  mean={mean:.3f}ms  p50={p50:.3f}ms  p95={p95:.3f}ms  p99={p99:.3f}ms  max={pmax:.3f}ms")
    return p50, p95, p99

print(f"Benchmarking {ITERS} iterations each against local Redis 5.0\n")

sw_keys  = lambda i, tag: [f"rl:sw:{tag}:{i % 50}", f"rl:sw:seq:{tag}:{i % 50}"]
sw_args  = lambda now: [now, 60000, 100]

fw_keys  = lambda i, tag: [f"rl:fw:{tag}:{i % 50}"]
fw_args  = lambda now: [100, 60, now]

tb_keys  = lambda i, tag: [f"rl:tb:{tag}:{i % 50}"]
tb_args  = lambda now: [100, 10.0, now]

lb_keys  = lambda i, tag: [f"rl:lb:{tag}:{i % 50}"]
lb_args  = lambda now: [100, 10.0, now]

results = {}
results["sliding_window"] = bench("sliding_window", scripts["sliding_window"], sw_keys, sw_args)
results["fixed_window"]   = bench("fixed_window",   scripts["fixed_window"],   fw_keys, fw_args)
results["token_bucket"]   = bench("token_bucket",   scripts["token_bucket"],   tb_keys, tb_args)
results["leaky_bucket"]   = bench("leaky_bucket",   scripts["leaky_bucket"],   lb_keys, lb_args)

all_p50 = [v[0] for v in results.values()]
all_p99 = [v[2] for v in results.values()]
print(f"\nWorst-case across all algorithms:  p50={max(all_p50):.3f}ms  p99={max(all_p99):.3f}ms")
print(f"Best-case across all algorithms:   p50={min(all_p50):.3f}ms  p99={min(all_p99):.3f}ms")
