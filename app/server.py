"""
This is a dummy service that demonstrates how to integrate with the rate limiter sidecar.
It has two endpoints, each protected by a different rate limiting algorithm.
"""
from enum import Enum
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
import httpx

app = FastAPI(
    title="Dummy Service",
)


class RateLimitAlgorithm(str, Enum):
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"


# Rate limiter service URL (the sidecar)
RATE_LIMITER_URL = "http://localhost:8000"


# Rate limit dependency factory - creates a dependency with specific config
def rate_limit(algorithm: RateLimitAlgorithm, limit: int = 5, window: int = 60):
    async def check_rate_limit(request: Request):
        identifier = request.client.host if request.client else "unknown"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{RATE_LIMITER_URL}/rate-limit/check",
                    json={
                        "identifier": identifier,
                        "algorithm": algorithm.value,
                        "limit": limit,
                        "window_seconds": window,
                    },
                    timeout=5.0,
                )

            if response.status_code != 200:
                return  # Fail open

            data = response.json()
            if not data.get("allowed", False):
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded",
                    headers={"Retry-After": str(data.get("retry_after", 60))},
                )

        except httpx.RequestError:
            pass  # Fail open

    return check_rate_limit


# Router with TOKEN BUCKET rate limiting
router = APIRouter(
    prefix="/dummy_token_bucket",
    dependencies=[
        Depends(rate_limit(RateLimitAlgorithm.TOKEN_BUCKET, limit=5, window=60))
    ],
)


@router.get("/")
async def health_check():
    return {"status": "healthy", "algorithm": "token_bucket"}


@router.get("/ping")
async def ping():
    return {"message": "pong", "algorithm": "token_bucket"}


# Router with SLIDING WINDOW rate limiting
router2 = APIRouter(
    prefix="/dummy_sliding_window",
    dependencies=[
        Depends(rate_limit(RateLimitAlgorithm.SLIDING_WINDOW, limit=10, window=60))
    ],
)


@router2.get("/")
async def health_check_sw():
    return {"status": "healthy", "algorithm": "sliding_window"}


@router2.get("/ping")
async def ping_sw():
    return {"message": "pong", "algorithm": "sliding_window"}


app.include_router(router)
app.include_router(router2)


# run app
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=5000, host="0.0.0.0")
