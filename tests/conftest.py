"""
Pytest configuration and fixtures for rate limiter tests.
"""

import pytest
import pytest_asyncio
import fakeredis.aioredis
from unittest.mock import patch, AsyncMock

from app.redis_client import RedisClient, redis_client
from app.config import Settings


@pytest.fixture
def settings():
    """Override settings for testing."""
    return Settings(
        redis_host="localhost",
        redis_port=6379,
        redis_db=15,  # Use different DB for tests
        default_limit=100,
        default_window_seconds=60,
        log_level="DEBUG",
    )


@pytest_asyncio.fixture
async def fake_redis():
    """
    Create a fake Redis instance for testing.
    Uses fakeredis for in-memory Redis simulation with Lua scripting support.
    """
    # Create FakeRedis with Lua scripting enabled
    fake = fakeredis.aioredis.FakeRedis(
        decode_responses=True, lua_modules={"cjson", "struct", "cmsgpack", "bit"}
    )
    yield fake
    await fake.aclose()


@pytest_asyncio.fixture
async def mock_redis_client(fake_redis):
    """
    Mock the global Redis client with fake Redis.
    """
    # Create a mock for the redis_client singleton
    original_client = redis_client._client
    original_pool = redis_client._pool

    redis_client._client = fake_redis
    redis_client._pool = AsyncMock()

    yield redis_client

    # Restore original
    redis_client._client = original_client
    redis_client._pool = original_pool


@pytest_asyncio.fixture
async def test_client(mock_redis_client):
    """
    Create a test client for the FastAPI application.
    """
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    # Skip actual Redis connection in lifespan
    async def mock_connect():
        pass

    async def mock_disconnect():
        pass

    with patch.object(redis_client, "connect", mock_connect):
        with patch.object(redis_client, "disconnect", mock_disconnect):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                yield client
