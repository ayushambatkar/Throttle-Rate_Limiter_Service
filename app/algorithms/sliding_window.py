"""
Sliding Window Log Algorithm Implementation.

The Sliding Window Log algorithm works by:
1. Storing timestamps of all requests within the window
2. For each new request, remove timestamps older than the window
3. Count remaining timestamps
4. If count < limit, allow request and add current timestamp
5. Otherwise, deny the request

This provides more accurate rate limiting than fixed windows
but uses more memory (O(n) where n is the limit).

This implementation uses Redis Sorted Sets with Lua scripts for atomic operations.
"""

import time
import logging
from dataclasses import dataclass
from typing import Optional

from app.redis_client import redis_client

logger = logging.getLogger(__name__)


@dataclass
class SlidingWindowResult:
    """Result of a sliding window rate limit check."""

    allowed: bool
    remaining: int
    reset_in_seconds: float
    retry_after: Optional[float]


# Lua script for atomic sliding window operations
# Uses sorted set with timestamp as score for efficient range queries
SLIDING_WINDOW_LUA_SCRIPT = """
-- Sliding Window Log Rate Limiter Lua Script
-- KEYS[1]: window key (e.g., "sliding_window:user_123")
-- ARGV[1]: limit (max requests per window)
-- ARGV[2]: window_seconds
-- ARGV[3]: current timestamp (milliseconds for precision)

local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Calculate window start time
local window_start = now - (window_seconds * 1000)

-- Remove expired entries (timestamps before window start)
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Count current requests in window
local current_count = redis.call('ZCARD', key)

-- Check if we can allow this request
local allowed = 0
local remaining = limit - current_count
local retry_after = 0

if current_count < limit then
    -- Add new request timestamp
    -- Use timestamp + random suffix for uniqueness (handles same-ms requests)
    local member = tostring(now) .. ':' .. tostring(math.random(1000000))
    redis.call('ZADD', key, now, member)
    allowed = 1
    remaining = limit - current_count - 1
else
    -- Request denied - calculate when oldest request expires
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if #oldest >= 2 then
        local oldest_time = tonumber(oldest[2])
        retry_after = ((oldest_time + (window_seconds * 1000)) - now) / 1000
        if retry_after < 0 then
            retry_after = 0
        end
    end
    remaining = 0
end

-- Set TTL on the key (window + buffer for cleanup)
redis.call('EXPIRE', key, math.ceil(window_seconds * 2))

-- Calculate reset time (when window fully resets)
local oldest_in_window = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local reset_in_seconds = window_seconds
if #oldest_in_window >= 2 then
    local oldest_time = tonumber(oldest_in_window[2])
    reset_in_seconds = ((oldest_time + (window_seconds * 1000)) - now) / 1000
    if reset_in_seconds < 0 then
        reset_in_seconds = 0
    end
end

-- Return: allowed, remaining, reset_in_seconds, retry_after
return {allowed, remaining, string.format("%.3f", reset_in_seconds), string.format("%.3f", retry_after)}
"""

# Lua script for getting window status without adding a request
SLIDING_WINDOW_STATUS_LUA_SCRIPT = """
-- Sliding Window Status Check (no consumption)
-- KEYS[1]: window key
-- ARGV[1]: limit
-- ARGV[2]: window_seconds
-- ARGV[3]: current timestamp (milliseconds)

local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Calculate window start time
local window_start = now - (window_seconds * 1000)

-- Remove expired entries first
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Count current requests in window
local current_count = redis.call('ZCARD', key)
local remaining = math.max(0, limit - current_count)

-- Calculate reset time
local reset_in_seconds = window_seconds
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if #oldest >= 2 then
    local oldest_time = tonumber(oldest[2])
    reset_in_seconds = ((oldest_time + (window_seconds * 1000)) - now) / 1000
    if reset_in_seconds < 0 then
        reset_in_seconds = 0
    end
end

return {current_count, remaining, string.format("%.3f", reset_in_seconds)}
"""


class SlidingWindowLimiter:
    """
    Sliding Window Log Rate Limiter implementation using Redis.

    This algorithm tracks the exact timestamp of each request
    within the window, providing accurate rate limiting without
    the boundary issues of fixed windows.

    Trade-offs:
    - More accurate than fixed/sliding window counters
    - Higher memory usage (stores each request timestamp)
    - Uses Redis Sorted Sets for efficient range operations
    """

    KEY_PREFIX = "sliding_window"

    @classmethod
    def _get_key(cls, identifier: str) -> str:
        """Generate Redis key for an identifier."""
        return f"{cls.KEY_PREFIX}:{identifier}"

    @classmethod
    async def check(
        cls, identifier: str, limit: int, window_seconds: int
    ) -> SlidingWindowResult:
        """
        Check rate limit and record request if allowed.

        Args:
            identifier: Unique identifier (user_id, IP, etc.)
            limit: Maximum requests per window
            window_seconds: Time window in seconds

        Returns:
            SlidingWindowResult with allowed status and metadata
        """
        key = cls._get_key(identifier)

        # Use milliseconds for better precision
        current_time_ms = int(time.time() * 1000)

        try:
            result = await redis_client.execute_lua_script(
                SLIDING_WINDOW_LUA_SCRIPT,
                keys=[key],
                args=[limit, window_seconds, current_time_ms],
            )

            allowed = bool(int(result[0]))
            remaining = int(result[1])
            reset_in_seconds = float(result[2])
            retry_after = float(result[3]) if not allowed else None

            logger.debug(
                f"Sliding window check for {identifier}: "
                f"allowed={allowed}, remaining={remaining}"
            )

            return SlidingWindowResult(
                allowed=allowed,
                remaining=remaining,
                reset_in_seconds=reset_in_seconds,
                retry_after=retry_after,
            )

        except Exception as e:
            logger.error(f"Sliding window check failed for {identifier}: {e}")
            raise

    @classmethod
    async def get_status(cls, identifier: str, limit: int, window_seconds: int) -> dict:
        """
        Get current rate limit status without recording a request.

        Args:
            identifier: Unique identifier
            limit: Maximum requests per window
            window_seconds: Time window in seconds

        Returns:
            Dict with requests_used, remaining, and reset_in_seconds
        """
        key = cls._get_key(identifier)
        current_time_ms = int(time.time() * 1000)

        try:
            result = await redis_client.execute_lua_script(
                SLIDING_WINDOW_STATUS_LUA_SCRIPT,
                keys=[key],
                args=[limit, window_seconds, current_time_ms],
            )

            return {
                "requests_used": int(result[0]),
                "remaining": int(result[1]),
                "reset_in_seconds": float(result[2]),
            }

        except Exception as e:
            logger.error(f"Sliding window status check failed for {identifier}: {e}")
            raise

    @classmethod
    async def reset(cls, identifier: str) -> bool:
        """
        Reset rate limit for an identifier.

        Args:
            identifier: Unique identifier to reset

        Returns:
            True if reset was successful, False if key didn't exist
        """
        key = cls._get_key(identifier)

        try:
            deleted = await redis_client.delete_key(key)
            logger.info(f"Reset sliding window for {identifier}: deleted={deleted}")
            return deleted

        except Exception as e:
            logger.error(f"Failed to reset sliding window for {identifier}: {e}")
            raise
