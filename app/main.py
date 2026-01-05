"""
Distributed Rate Limiter API

A production-ready rate limiting service using FastAPI and Redis.
Supports Token Bucket and Sliding Window Log algorithms.
"""

import logging
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from pythonjsonlogger import jsonlogger

from .config import get_settings
from app.models import (
    Algorithm,
    RateLimitCheckRequest,
    RateLimitCheckResponse,
    RateLimitStatusResponse,
    RateLimitResetResponse,
    HealthResponse,
    ErrorResponse,
)
from app.redis_client import redis_client
from app.algorithms import TokenBucketLimiter, SlidingWindowLimiter


# Configure logging
def setup_logging():
    """Configure structured JSON logging."""
    settings = get_settings()

    logger = logging.getLogger()
    logger.setLevel(getattr(logging, settings.log_level.upper()))

    handler = logging.StreamHandler(sys.stdout)

    if settings.log_format == "json":
        formatter = jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Reduce noise from other loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Handles startup and shutdown of Redis connection.
    """
    # Startup
    logger.info("Starting Rate Limiter API...")
    try:
        await redis_client.connect()
        logger.info("Rate Limiter API started successfully")
    except RedisError as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise

    yield

    # Shutdown
    logger.info("Shutting down Rate Limiter API...")
    await redis_client.disconnect()
    logger.info("Rate Limiter API shutdown complete")


# Create FastAPI application
settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="""
## Distributed Rate Limiter API

A production-ready rate limiting service supporting multiple algorithms.

### Features
- **Token Bucket Algorithm**: Allows burst traffic while maintaining average rate
- **Sliding Window Log**: Precise rate limiting with request timestamp tracking
- **Atomic Operations**: Uses Redis Lua scripts to prevent race conditions
- **Distributed**: Backed by Redis for multi-instance deployments

### Usage
1. Use `/rate-limit/check` to verify and consume rate limit
2. Use `/rate-limit/status/{identifier}` to check current status
3. Use `/rate-limit/reset/{identifier}` to reset limits (admin use)
    """,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS middleware for cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Exception handlers
@app.exception_handler(RedisError)
async def redis_exception_handler(request: Request, exc: RedisError):
    """Handle Redis connection and operation errors."""
    logger.error(f"Redis error: {exc}", extra={"path": request.url.path})
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Rate limiting service temporarily unavailable",
            "error_code": "REDIS_ERROR",
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors."""
    logger.exception(f"Unexpected error: {exc}", extra={"path": request.url.path})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error_code": "INTERNAL_ERROR"},
    )


# Endpoints
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
    summary="Health check endpoint",
    description="Check if the service and Redis connection are healthy.",
)
async def health_check():
    """
    Health check endpoint for load balancers and orchestration.
    Returns service health status and Redis connectivity.
    """
    redis_healthy = await redis_client.is_healthy()

    status = "healthy" if redis_healthy else "unhealthy"

    if not redis_healthy:
        logger.warning("Health check failed: Redis not connected")

    return HealthResponse(
        status=status, redis_connected=redis_healthy, version=settings.app_version
    )


@app.post(
    "/rate-limit/check",
    response_model=RateLimitCheckResponse,
    responses={
        200: {"description": "Rate limit check successful"},
        429: {"description": "Rate limit exceeded", "model": RateLimitCheckResponse},
        503: {"description": "Service unavailable", "model": ErrorResponse},
    },
    tags=["Rate Limiting"],
    summary="Check and consume rate limit",
    description="""
Check if a request should be allowed based on rate limiting rules.
If allowed, consumes one unit from the rate limit.

**Algorithms:**
- `token_bucket`: Tokens refill at a constant rate, allows bursting
- `sliding_window`: Tracks exact request timestamps, more precise
    """,
)
async def check_rate_limit(request: RateLimitCheckRequest):
    """
    Check rate limit for an identifier and consume one request unit.

    This endpoint performs an atomic check-and-consume operation.
    If the request is allowed, it counts against the rate limit.
    """
    logger.info(
        f"Rate limit check",
        extra={
            "identifier": request.identifier,
            "algorithm": request.algorithm.value,
            "limit": request.limit,
            "window_seconds": request.window_seconds,
        },
    )

    try:
        if request.algorithm == Algorithm.TOKEN_BUCKET:
            result = await TokenBucketLimiter.check(
                identifier=request.identifier,
                limit=request.limit,
                window_seconds=request.window_seconds,
            )
        else:
            result = await SlidingWindowLimiter.check(
                identifier=request.identifier,
                limit=request.limit,
                window_seconds=request.window_seconds,
            )

        response = RateLimitCheckResponse(
            allowed=result.allowed,
            remaining=result.remaining,
            reset_in_seconds=round(result.reset_in_seconds, 2),
            retry_after=round(result.retry_after, 2) if result.retry_after else None,
        )

        # Log rate limit exceeded events
        if not result.allowed:
            logger.warning(
                f"Rate limit exceeded",
                extra={
                    "identifier": request.identifier,
                    "algorithm": request.algorithm.value,
                    "retry_after": result.retry_after,
                },
            )

        return response

    except RedisError:
        raise
    except Exception as e:
        logger.exception(f"Rate limit check failed: {e}")
        raise HTTPException(status_code=500, detail="Rate limit check failed")


@app.get(
    "/rate-limit/status/{identifier}",
    response_model=RateLimitStatusResponse,
    responses={
        200: {"description": "Status retrieved successfully"},
        503: {"description": "Service unavailable", "model": ErrorResponse},
    },
    tags=["Rate Limiting"],
    summary="Get rate limit status",
    description="""
Get the current rate limit status for an identifier without consuming any quota.
Useful for displaying remaining limits to users or monitoring.
    """,
)
async def get_rate_limit_status(
    identifier: str,
    algorithm: Algorithm = Query(
        default=Algorithm.TOKEN_BUCKET, description="Rate limiting algorithm used"
    ),
    limit: int = Query(
        default=100, ge=1, le=100000, description="Maximum requests per window"
    ),
    window_seconds: int = Query(
        default=60, ge=1, le=86400, description="Time window in seconds"
    ),
):
    """
    Get current rate limit status for an identifier.

    This endpoint does NOT consume any rate limit quota.
    It's safe to call frequently for monitoring or UI display.
    """
    logger.debug(f"Status check for {identifier}", extra={"algorithm": algorithm.value})

    try:
        if algorithm == Algorithm.TOKEN_BUCKET:
            status = await TokenBucketLimiter.get_status(
                identifier=identifier, limit=limit, window_seconds=window_seconds
            )
        else:
            status = await SlidingWindowLimiter.get_status(
                identifier=identifier, limit=limit, window_seconds=window_seconds
            )

        return RateLimitStatusResponse(
            identifier=identifier,
            requests_used=status["requests_used"],
            limit=limit,
            window_seconds=window_seconds,
            algorithm=algorithm,
            reset_in_seconds=round(status["reset_in_seconds"], 2),
        )

    except RedisError:
        raise
    except Exception as e:
        logger.exception(f"Status check failed: {e}")
        raise HTTPException(status_code=500, detail="Status check failed")


@app.delete(
    "/rate-limit/reset/{identifier}",
    response_model=RateLimitResetResponse,
    responses={
        200: {"description": "Rate limit reset successfully"},
        404: {"description": "Identifier not found"},
        503: {"description": "Service unavailable", "model": ErrorResponse},
    },
    tags=["Rate Limiting"],
    summary="Reset rate limit for an identifier",
    description="""
Reset the rate limit for a specific identifier.
This clears all rate limit data for both algorithms.

**Warning:** This is an admin operation. Consider adding authentication
in production deployments.
    """,
)
async def reset_rate_limit(
    identifier: str,
    algorithm: Optional[Algorithm] = Query(
        default=None,
        description="Specific algorithm to reset. If not provided, resets all.",
    ),
):
    """
    Reset rate limit for an identifier.

    This removes all rate limiting data for the identifier,
    effectively giving them a fresh quota.
    """
    logger.info(
        f"Resetting rate limit for {identifier}",
        extra={"algorithm": algorithm.value if algorithm else "all"},
    )

    try:
        reset_count = 0

        if algorithm is None or algorithm == Algorithm.TOKEN_BUCKET:
            if await TokenBucketLimiter.reset(identifier):
                reset_count += 1

        if algorithm is None or algorithm == Algorithm.SLIDING_WINDOW:
            if await SlidingWindowLimiter.reset(identifier):
                reset_count += 1

        if reset_count == 0 and algorithm is not None:
            raise HTTPException(
                status_code=404, detail=f"No rate limit data found for {identifier}"
            )

        return RateLimitResetResponse(message=f"Rate limit reset for {identifier}")

    except HTTPException:
        raise
    except RedisError:
        raise
    except Exception as e:
        logger.exception(f"Reset failed: {e}")
        raise HTTPException(status_code=500, detail="Reset failed")


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all incoming requests."""
    import time

    start_time = time.time()

    response = await call_next(request)

    process_time = time.time() - start_time

    logger.info(
        f"{request.method} {request.url.path}",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "process_time_ms": round(process_time * 1000, 2),
            "client_ip": request.client.host if request.client else None,
        },
    )

    # Add timing header
    response.headers["X-Process-Time"] = str(round(process_time * 1000, 2))

    return response


from fastapi.responses import HTMLResponse
@app.get("/")
async def root():
    return HTMLResponse(content="""
    <html>
        <head>
            <title>Throttle Rate Limiter</title>
        </head>
        <body>
            <h1>Throttle: Distributed Rate Limiter API</h1>
            <p>A production-ready rate limiting service using FastAPI and Redis.</p>
            <ul>
                <li><a href="/docs">API Documentation (Swagger UI)</a></li>
                <li><a href="/redoc">API Documentation (ReDoc)</a></li>
                <li><a href="/health">Health Check</a></li>
            </ul>
        </body>
    </html>
    """)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        # host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
