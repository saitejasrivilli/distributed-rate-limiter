from fastapi import HTTPException
from app.config import (
    CLIENT_ID_MAX_LEN, CLIENT_ID_PATTERN,
    MAX_LIMIT, MIN_LIMIT,
    MAX_WINDOW_SECONDS, MIN_WINDOW_SECONDS,
    MAX_CAPACITY, MAX_REFILL_RATE, MIN_REFILL_RATE,
)


def validate_client_id(client_id: str) -> str:
    """
    Sanitise client_id to prevent Redis key injection.

    Attackers may try:
      - Key separators:  "user:../../../admin"
      - Glob patterns:   "user*" or "rl:sw:*"
      - Very long keys:  memory exhaustion
      - Null bytes:      "\x00admin"
    """
    if not client_id or not client_id.strip():
        raise HTTPException(status_code=400, detail="client_id cannot be empty")

    if len(client_id) > CLIENT_ID_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"client_id must be ≤ {CLIENT_ID_MAX_LEN} characters"
        )

    if not CLIENT_ID_PATTERN.match(client_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "client_id may only contain letters, digits, "
                "hyphens, underscores, dots, @ and colons"
            )
        )

    return client_id


def validate_limit(limit: int) -> int:
    """Clamp limit to server-side bounds — callers cannot set limit=999999."""
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}"
        )
    return limit


def validate_window(window_seconds: int) -> int:
    if window_seconds < MIN_WINDOW_SECONDS or window_seconds > MAX_WINDOW_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"window_seconds must be between {MIN_WINDOW_SECONDS} and {MAX_WINDOW_SECONDS}"
        )
    return window_seconds


def validate_capacity(capacity: int) -> int:
    if capacity < MIN_LIMIT or capacity > MAX_CAPACITY:
        raise HTTPException(
            status_code=400,
            detail=f"capacity must be between {MIN_LIMIT} and {MAX_CAPACITY}"
        )
    return capacity


def validate_refill_rate(rate: float) -> float:
    """
    MIN_REFILL_RATE prevents near-zero values that would cause
    extremely long waits (or division issues) in the Lua script.
    """
    if rate < MIN_REFILL_RATE or rate > MAX_REFILL_RATE:
        raise HTTPException(
            status_code=400,
            detail=f"refill_rate must be between {MIN_REFILL_RATE} and {MAX_REFILL_RATE}"
        )
    return rate
