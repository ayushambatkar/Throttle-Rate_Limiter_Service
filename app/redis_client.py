"""
Redis client with connection pooling and error handling.
Provides a singleton Redis connection for the application.
"""

import logging
from typing import Optional
from contextlib import asynccontextmanager

import redis.asyncio as redis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError

from app.config import get_settings

logger = logging.getLogger(__name__)


class RedisClient:
    """
    Async Redis client wrapper with connection pooling and health checks.

    This class manages a Redis connection pool and provides methods for
    common operations with proper error handling.
    """

    _instance: Optional["RedisClient"] = None
    _pool: Optional[ConnectionPool] = None
    _client: Optional[redis.Redis] = None

    def __new__(cls) -> "RedisClient":
        """Singleton pattern to ensure single connection pool."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def connect(self) -> None:
        """
        Initialize the Redis connection pool.
        Should be called during application startup.
        """
        if self._client is not None:
            return

        settings = get_settings()

        try:
            self._pool = ConnectionPool.from_url(
                settings.redis_connection_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                retry_on_timeout=settings.redis_retry_on_timeout,
                decode_responses=True,
            )

            self._client = redis.Redis(connection_pool=self._pool)

            # Verify connection
            await self._client.ping()
            logger.info(
                "Connected to Redis",
                extra={
                    "host": settings.redis_host,
                    "port": settings.redis_port,
                    "db": settings.redis_db,
                },
            )

        except RedisConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    async def disconnect(self) -> None:
        """
        Close the Redis connection pool.
        Should be called during application shutdown.
        """
        if self._client:
            await self._client.aclose()
            self._client = None

        if self._pool:
            await self._pool.disconnect()
            self._pool = None

        logger.info("Disconnected from Redis")

    @property
    def client(self) -> redis.Redis:
        """Get the Redis client instance."""
        if self._client is None:
            raise RuntimeError("Redis client not initialized. Call connect() first.")
        return self._client

    async def is_healthy(self) -> bool:
        """
        Check if Redis connection is healthy.

        Returns:
            True if Redis is reachable, False otherwise.
        """
        try:
            if self._client is None:
                return False
            await self._client.ping()
            return True
        except RedisError:
            return False

    async def execute_lua_script(self, script: str, keys: list[str], args: list) -> any:
        """
        Execute a Lua script atomically.

        Args:
            script: The Lua script to execute
            keys: List of Redis keys used in the script
            args: List of arguments to pass to the script

        Returns:
            The result of the script execution

        Raises:
            RedisError: If script execution fails
        """
        try:
            return await self.client.eval(script, len(keys), *keys, *args)
        except RedisError as e:
            logger.error(f"Lua script execution failed: {e}")
            raise

    async def get_key(self, key: str) -> Optional[str]:
        """Get a value from Redis."""
        try:
            return await self.client.get(key)
        except RedisError as e:
            logger.error(f"Failed to get key {key}: {e}")
            raise

    async def delete_key(self, key: str) -> bool:
        """
        Delete a key from Redis.

        Returns:
            True if key was deleted, False if key didn't exist.
        """
        try:
            result = await self.client.delete(key)
            return result > 0
        except RedisError as e:
            logger.error(f"Failed to delete key {key}: {e}")
            raise

    async def delete_pattern(self, pattern: str) -> int:
        """
        Delete all keys matching a pattern.

        Args:
            pattern: Redis key pattern (e.g., "token_bucket:user_*")

        Returns:
            Number of keys deleted.
        """
        try:
            keys = []
            async for key in self.client.scan_iter(match=pattern):
                keys.append(key)

            if keys:
                return await self.client.delete(*keys)
            return 0
        except RedisError as e:
            logger.error(f"Failed to delete pattern {pattern}: {e}")
            raise


# Global Redis client instance
redis_client = RedisClient()


async def get_redis() -> redis.Redis:
    """
    Dependency injection for Redis client.
    Use this in FastAPI route dependencies.
    """
    return redis_client.client


@asynccontextmanager
async def redis_lifespan():
    """
    Context manager for Redis connection lifecycle.
    Use this in FastAPI lifespan.
    """
    await redis_client.connect()
    try:
        yield
    finally:
        await redis_client.disconnect()
