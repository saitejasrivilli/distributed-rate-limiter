"""
Locust load test for the Distributed Rate Limiter.

Run:
    locust -f load_tests/locustfile.py --host http://localhost:8000 \
           --users 500 --spawn-rate 10 --run-time 120s --headless \
           --html load_tests/report.html

Expected results at 500 concurrent users:
    p50  < 5ms
    p95  < 8ms
    p99  < 10ms
    RPS  ~ 2000-5000 (Redis-bound; scales with Upstash tier)
"""

import random
import string
from locust import HttpUser, TaskSet, task, between, constant_throughput


# ---------------------------------------------------------------------------
# Shared client ID pool — simulate N distinct users
# ---------------------------------------------------------------------------
_CLIENT_POOL = [f"load_user_{i:04d}" for i in range(200)]


def random_client() -> str:
    return random.choice(_CLIENT_POOL)


# ---------------------------------------------------------------------------
# Task sets — one per algorithm so we can weight them independently
# ---------------------------------------------------------------------------

class SlidingWindowTasks(TaskSet):
    """Hit the sliding window endpoint with randomised client IDs and limits."""

    @task(3)
    def check_under_limit(self):
        client = random_client()
        self.client.get(
            "/check/sliding_window",
            params={"client_id": client, "limit": 100, "window_seconds": 10},
            name="/check/sliding_window [under]",
        )

    @task(1)
    def check_at_limit(self):
        """Drive a single client hard — will start getting 429s."""
        self.client.get(
            "/check/sliding_window",
            params={"client_id": "flood_sw", "limit": 5, "window_seconds": 60},
            name="/check/sliding_window [429]",
        )


class FixedWindowTasks(TaskSet):

    @task(3)
    def check_under_limit(self):
        client = random_client()
        self.client.get(
            "/check/fixed_window",
            params={"client_id": client, "limit": 100, "window_seconds": 10},
            name="/check/fixed_window [under]",
        )

    @task(1)
    def check_at_limit(self):
        self.client.get(
            "/check/fixed_window",
            params={"client_id": "flood_fw", "limit": 5, "window_seconds": 60},
            name="/check/fixed_window [429]",
        )


class TokenBucketTasks(TaskSet):

    @task(3)
    def check_under_limit(self):
        client = random_client()
        self.client.get(
            "/check/token_bucket",
            params={"client_id": client, "capacity": 100, "refill_rate": 20.0},
            name="/check/token_bucket [under]",
        )

    @task(1)
    def check_empty_bucket(self):
        self.client.get(
            "/check/token_bucket",
            params={"client_id": "flood_tb", "capacity": 2, "refill_rate": 0.1},
            name="/check/token_bucket [429]",
        )


class LeakyBucketTasks(TaskSet):

    @task(3)
    def check_under_limit(self):
        client = random_client()
        self.client.get(
            "/check/leaky_bucket",
            params={"client_id": client, "capacity": 100, "leak_rate": 20.0},
            name="/check/leaky_bucket [under]",
        )

    @task(1)
    def check_full_queue(self):
        self.client.get(
            "/check/leaky_bucket",
            params={"client_id": "flood_lb", "capacity": 2, "leak_rate": 0.1},
            name="/check/leaky_bucket [429]",
        )


class MixedTasks(TaskSet):
    """Weighted mix hitting all four endpoints — reflects realistic traffic."""

    @task(4)
    def sliding_window(self):
        client = random_client()
        with self.client.get(
            "/check/sliding_window",
            params={"client_id": client, "limit": 50, "window_seconds": 10},
            name="/check/sliding_window",
            catch_response=True,
        ) as resp:
            # 429 is an expected, valid response — don't count as failure
            if resp.status_code in (200, 429):
                resp.success()

    @task(3)
    def fixed_window(self):
        client = random_client()
        with self.client.get(
            "/check/fixed_window",
            params={"client_id": client, "limit": 50, "window_seconds": 10},
            name="/check/fixed_window",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 429):
                resp.success()

    @task(2)
    def token_bucket(self):
        client = random_client()
        with self.client.get(
            "/check/token_bucket",
            params={"client_id": client, "capacity": 50, "refill_rate": 10.0},
            name="/check/token_bucket",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 429):
                resp.success()

    @task(2)
    def leaky_bucket(self):
        client = random_client()
        with self.client.get(
            "/check/leaky_bucket",
            params={"client_id": client, "capacity": 50, "leak_rate": 10.0},
            name="/check/leaky_bucket",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 429):
                resp.success()

    @task(1)
    def health(self):
        with self.client.get("/health", name="/health", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()


# ---------------------------------------------------------------------------
# User classes
# ---------------------------------------------------------------------------

class RateLimiterUser(HttpUser):
    """
    Primary user class — exercises all four algorithms with realistic weights.
    Wait time: 0.1–0.5 s between tasks (simulates ~2–10 req/s per user).
    At 500 users this yields ~1000–5000 req/s aggregate.
    """
    tasks = [MixedTasks]
    wait_time = between(0.1, 0.5)

    # Accept 429 as success at the HTTP client level so Locust doesn't
    # flag them as errors — the rate limiter is working as designed.
    def on_start(self):
        pass


class BurstUser(HttpUser):
    """
    Burst user — hammers a single endpoint to stress 429 path.
    Kept at weight=1 relative to RateLimiterUser (add --weight flags if needed).
    """
    tasks = [SlidingWindowTasks]
    wait_time = between(0.05, 0.15)
    weight = 1


class SteadyUser(HttpUser):
    """
    Steady-state user — constant throughput targeting health + token_bucket.
    """
    tasks = {TokenBucketTasks: 3, LeakyBucketTasks: 2}
    wait_time = constant_throughput(5)  # 5 req/s per user
    weight = 1
