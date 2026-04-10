import os
import re
import secrets
from typing import Final

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------
# The ADMIN_API_KEY protects destructive endpoints (reset, admin stats).
# Set via environment variable. If not set, a random key is generated at
# startup (printed to logs once) so the service is never accidentally open.
ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")
_generated_key: str = ""

def get_admin_key() -> str:
    global _generated_key
    if ADMIN_API_KEY:
        return ADMIN_API_KEY
    if not _generated_key:
        _generated_key = secrets.token_urlsafe(32)
    return _generated_key


# ---------------------------------------------------------------------------
# Input validation constants
# ---------------------------------------------------------------------------
CLIENT_ID_MAX_LEN: Final[int] = 128
CLIENT_ID_PATTERN: Final[re.Pattern] = re.compile(r"^[a-zA-Z0-9_\-\.@:]+$")

# Hard server-side caps — callers cannot exceed these regardless of what they send.
# This prevents "set limit=999999 to bypass your own rate limit" attacks.
MAX_LIMIT: Final[int] = 10_000
MIN_LIMIT: Final[int] = 1
MAX_WINDOW_SECONDS: Final[int] = 86_400   # 24 hours
MIN_WINDOW_SECONDS: Final[int] = 1
MAX_CAPACITY: Final[int] = 10_000
MAX_REFILL_RATE: Final[float] = 1_000.0
MIN_REFILL_RATE: Final[float] = 0.01      # prevents near-zero division in Lua

# Simulate endpoint — cap request count to prevent Redis flood
MAX_SIMULATE_REQUESTS: Final[int] = 100

# ---------------------------------------------------------------------------
# Redis connection config
# ---------------------------------------------------------------------------
REDIS_CONNECT_TIMEOUT: Final[int] = 5     # seconds
REDIS_SOCKET_TIMEOUT: Final[int] = 3      # seconds per command
REDIS_MAX_CONNECTIONS: Final[int] = 20    # pool size
REDIS_HEALTH_CHECK_INTERVAL: Final[int] = 30  # seconds

# ---------------------------------------------------------------------------
# Self-protection: rate-limit the rate-limiter's own API
# These limits apply globally (all clients combined) on the /check endpoints
# to prevent the service itself from being DoS'd via the Swagger UI.
# ---------------------------------------------------------------------------
SELF_PROTECT_LIMIT: Final[int] = 500       # requests per window
SELF_PROTECT_WINDOW: Final[int] = 10       # seconds
SIMULATE_PROTECT_LIMIT: Final[int] = 20    # simulate calls per window
SIMULATE_PROTECT_WINDOW: Final[int] = 60   # seconds
