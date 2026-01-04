"""
Pydantic models for Rate Limiter API requests and responses.
"""

from typing import Optional, Literal
from pydantic import BaseModel, Field
from enum import Enum


class Algorithm(str, Enum):
    """Supported rate limiting algorithms."""

    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"


class RateLimitCheckRequest(BaseModel):
    """Request model for rate limit check endpoint."""

    identifier: str = Field(
        ...,
        description="Unique identifier for rate limiting (user_id, IP, API key, etc.)",
        examples=["user_123", "192.168.1.1", "api_key_abc"],
    )
    algorithm: Algorithm = Field(
        default=Algorithm.TOKEN_BUCKET, description="Rate limiting algorithm to use"
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=100000,
        description="Maximum number of requests allowed in the window",
    )
    window_seconds: int = Field(
        default=60, ge=1, le=86400, description="Time window in seconds"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "identifier": "user_123",
                    "algorithm": "token_bucket",
                    "limit": 100,
                    "window_seconds": 60,
                }
            ]
        }
    }


class RateLimitCheckResponse(BaseModel):
    """Response model for rate limit check endpoint."""

    allowed: bool = Field(description="Whether the request is allowed")
    remaining: int = Field(
        ge=0, description="Number of remaining requests in the current window"
    )
    reset_in_seconds: float = Field(
        ge=0, description="Seconds until the rate limit resets"
    )
    retry_after: Optional[float] = Field(
        default=None,
        description="Seconds to wait before retrying (only set when request is denied)",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "allowed": True,
                    "remaining": 45,
                    "reset_in_seconds": 30.5,
                    "retry_after": None,
                }
            ]
        }
    }


class RateLimitStatusResponse(BaseModel):
    """Response model for rate limit status endpoint."""

    identifier: str = Field(description="The identifier being rate limited")
    requests_used: int = Field(
        ge=0, description="Number of requests used in the current window"
    )
    limit: int = Field(ge=1, description="Maximum allowed requests")
    window_seconds: int = Field(ge=1, description="Time window in seconds")
    algorithm: Algorithm = Field(description="Algorithm used for this identifier")
    reset_in_seconds: float = Field(
        ge=0, description="Seconds until the rate limit resets"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "identifier": "user_123",
                    "requests_used": 55,
                    "limit": 100,
                    "window_seconds": 60,
                    "algorithm": "token_bucket",
                    "reset_in_seconds": 45.2,
                }
            ]
        }
    }


class RateLimitResetResponse(BaseModel):
    """Response model for rate limit reset endpoint."""

    message: str = Field(description="Confirmation message")

    model_config = {
        "json_schema_extra": {
            "examples": [{"message": "Rate limit reset for user_123"}]
        }
    }


class HealthResponse(BaseModel):
    """Response model for health check endpoint."""

    status: Literal["healthy", "unhealthy"] = Field(
        description="Health status of the service"
    )
    redis_connected: bool = Field(description="Whether Redis is connected")
    version: str = Field(description="API version")


class ErrorResponse(BaseModel):
    """Response model for error responses."""

    detail: str = Field(description="Error message")
    error_code: Optional[str] = Field(
        default=None, description="Machine-readable error code"
    )
