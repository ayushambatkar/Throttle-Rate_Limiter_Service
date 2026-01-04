"""
Token Bucket Algorithm Implementation.

The Token Bucket algorithm works by:
1. Maintaining a bucket of tokens for each identifier
2. Tokens are added at a constant rate (refill rate)
3. Each request consumes one token
4. If no tokens available, request is denied
5. Bucket has a maximum capacity (burst limit)

This implementation uses Redis with Lua scripts for atomic operations.
"""

import time
import logging
from dataclasses import dataclass
from typing import Optional

from app.redis_client import redis_client

logger = logging.getLogger(__name__)


@dataclass
class TokenBucketResult:
    """Result of a token bucket rate limit check."""

    allowed: bool
    remaining: int
    reset_in_seconds: float
    retry_after: Optional[float]


# Lua script for atomic token bucket operations
# This script handles token refill and consumption in a single atomic operation
TOKEN_BUCKET_LUA_SCRIPT = """
-- Token Bucket Rate Limiter Lua Script
-- KEYS[1]: bucket key (e.g., "token_bucket:user_123")
-- ARGV[1]: max tokens (bucket capacity / limit)
-- ARGV[2]: refill rate (tokens per second)
-- ARGV[3]: current timestamp (seconds with decimal)
-- ARGV[4]: window_seconds (for calculating reset time)

local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local window_seconds = tonumber(ARGV[4])

-- Get current bucket state
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

-- Initialize bucket if it doesn't exist
if tokens == nil then
    tokens = max_tokens
    last_refill = now
end

-- Calculate token refill
local time_passed = now - last_refill
local tokens_to_add = time_passed * refill_rate

-- Add tokens (capped at max)
tokens = math.min(max_tokens, tokens + tokens_to_add)
last_refill = now

-- Try to consume a token
local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

-- Calculate remaining tokens (floor to integer)
local remaining = math.floor(tokens)

-- Calculate reset time (when bucket would be full again)
local tokens_needed = max_tokens - tokens
local reset_in_seconds = 0
if tokens_needed > 0 and refill_rate > 0 then
    reset_in_seconds = tokens_needed / refill_rate
end

-- Calculate retry_after (when at least 1 token would be available)
local retry_after = 0
if allowed == 0 and refill_rate > 0 then
    retry_after = (1 - tokens) / refill_rate
end

-- Update bucket state with TTL
-- TTL is set to window_seconds + buffer to auto-cleanup inactive users
local ttl = math.ceil(window_seconds * 2)
redis.call('HSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(last_refill))
redis.call('EXPIRE', key, ttl)

-- Return: allowed, remaining, reset_in_seconds, retry_after
return {allowed, remaining, string.format("%.3f", reset_in_seconds), string.format("%.3f", retry_after)}
"""

# Lua script for getting bucket status without consuming tokens
TOKEN_BUCKET_STATUS_LUA_SCRIPT = """
-- Token Bucket Status Check (no consumption)
-- KEYS[1]: bucket key
-- ARGV[1]: max tokens
-- ARGV[2]: refill rate
-- ARGV[3]: current timestamp

local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- Get current bucket state
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

-- If bucket doesn't exist, return full capacity
if tokens == nil then
    return {0, max_tokens, "0.000"}
end

-- Calculate current tokens (with refill)
local time_passed = now - last_refill
local tokens_to_add = time_passed * refill_rate
tokens = math.min(max_tokens, tokens + tokens_to_add)

-- Calculate used tokens
local used = math.floor(max_tokens - tokens)
local remaining = math.floor(tokens)

-- Calculate reset time
local tokens_needed = max_tokens - tokens
local reset_in_seconds = 0
if tokens_needed > 0 and refill_rate > 0 then
    reset_in_seconds = tokens_needed / refill_rate
end

return {used, remaining, string.format("%.3f", reset_in_seconds)}
"""


class TokenBucketLimiter:
    """
    Token Bucket Rate Limiter implementation using Redis.

    The token bucket algorithm allows for burst traffic while
    maintaining an average rate limit. Tokens are added at a
    constant rate and consumed with each request.
    """

    KEY_PREFIX = "token_bucket"

    @classmethod
    def _get_key(cls, identifier: str) -> str:
        """Generate Redis key for an identifier."""
        return f"{cls.KEY_PREFIX}:{identifier}"

    @classmethod
    async def check(
        cls, identifier: str, limit: int, window_seconds: int
    ) -> TokenBucketResult:
        """
        Check rate limit and consume a token if available.

        Args:
            identifier: Unique identifier (user_id, IP, etc.)
            limit: Maximum tokens (requests) per window
            window_seconds: Time window in seconds

        Returns:
            TokenBucketResult with allowed status and metadata
        """
        key = cls._get_key(identifier)

        # Calculate refill rate (tokens per second)
        refill_rate = limit / window_seconds
        current_time = time.time()

        try:
            result = await redis_client.execute_lua_script(
                TOKEN_BUCKET_LUA_SCRIPT,
                keys=[key],
                args=[limit, refill_rate, current_time, window_seconds],
            )

            allowed = bool(int(result[0]))
            remaining = int(result[1])
            reset_in_seconds = float(result[2])
            retry_after = float(result[3]) if not allowed else None

            logger.debug(
                f"Token bucket check for {identifier}: "
                f"allowed={allowed}, remaining={remaining}"
            )

            return TokenBucketResult(
                allowed=allowed,
                remaining=remaining,
                reset_in_seconds=reset_in_seconds,
                retry_after=retry_after,
            )

        except Exception as e:
            logger.error(f"Token bucket check failed for {identifier}: {e}")
            raise

    @classmethod
    async def get_status(cls, identifier: str, limit: int, window_seconds: int) -> dict:
        """
        Get current rate limit status without consuming a token.

        Args:
            identifier: Unique identifier
            limit: Maximum tokens per window
            window_seconds: Time window in seconds

        Returns:
            Dict with requests_used, remaining, and reset_in_seconds
        """
        key = cls._get_key(identifier)
        refill_rate = limit / window_seconds
        current_time = time.time()

        try:
            result = await redis_client.execute_lua_script(
                TOKEN_BUCKET_STATUS_LUA_SCRIPT,
                keys=[key],
                args=[limit, refill_rate, current_time],
            )

            return {
                "requests_used": int(result[0]),
                "remaining": int(result[1]),
                "reset_in_seconds": float(result[2]),
            }

        except Exception as e:
            logger.error(f"Token bucket status check failed for {identifier}: {e}")
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
            logger.info(f"Reset token bucket for {identifier}: deleted={deleted}")
            return deleted

        except Exception as e:
            logger.error(f"Failed to reset token bucket for {identifier}: {e}")
            raise
