from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from enum import Enum
from app.config import (
    MAX_LIMIT, MIN_LIMIT, MAX_WINDOW_SECONDS, MIN_WINDOW_SECONDS,
    MAX_CAPACITY, MAX_REFILL_RATE, MIN_REFILL_RATE, MAX_SIMULATE_REQUESTS,
    CLIENT_ID_MAX_LEN, CLIENT_ID_PATTERN,
)


class AlgorithmType(str, Enum):
    sliding_window = "sliding_window"
    fixed_window = "fixed_window"
    token_bucket = "token_bucket"


class RateLimitResponse(BaseModel):
    allowed: bool = Field(..., description="Whether the request is permitted")
    remaining: int = Field(..., description="Requests remaining in current window/bucket")
    limit: int = Field(..., description="Max requests allowed")
    retry_after: Optional[int] = Field(
        None,
        description="Seconds to wait before retrying (only present when blocked)"
    )
    algorithm: str = Field(..., description="Algorithm used")
    client_id: str = Field(..., description="Client identifier")

    model_config = {
        "json_schema_extra": {
            "example": {
                "allowed": True,
                "remaining": 7,
                "limit": 10,
                "retry_after": None,
                "algorithm": "sliding_window",
                "client_id": "demo_user"
            }
        }
    }


class SimulateRequest(BaseModel):
    algorithm: AlgorithmType = Field(AlgorithmType.sliding_window)
    client_id: str = Field("simulate_user", max_length=CLIENT_ID_MAX_LEN)
    request_count: int = Field(15, ge=1, le=MAX_SIMULATE_REQUESTS)
    limit: int = Field(10, ge=MIN_LIMIT, le=MAX_LIMIT)
    window_seconds: int = Field(60, ge=MIN_WINDOW_SECONDS, le=MAX_WINDOW_SECONDS)
    capacity: int = Field(10, ge=MIN_LIMIT, le=MAX_CAPACITY)
    refill_rate: float = Field(1.0, ge=MIN_REFILL_RATE, le=MAX_REFILL_RATE)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("client_id cannot be empty")
        if not CLIENT_ID_PATTERN.match(v):
            raise ValueError(
                "client_id may only contain letters, digits, "
                "hyphens, underscores, dots, @ and colons"
            )
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "algorithm": "sliding_window",
                "client_id": "recruiter_test",
                "request_count": 15,
                "limit": 10,
                "window_seconds": 60
            }
        }
    }


class SimulateResult(BaseModel):
    request_number: int
    allowed: bool
    remaining: int
    retry_after: Optional[int]


class SimulateResponse(BaseModel):
    algorithm: str
    client_id: str
    total_requests: int
    allowed: int
    blocked: int
    block_rate_pct: float
    results: List[SimulateResult]


class StatusResponse(BaseModel):
    status: str
    redis_connected: bool
    algorithms_available: List[str]
    message: str


class ErrorResponse(BaseModel):
    error: str
    detail: str
    request_id: Optional[str] = None
