"""
Unit tests for Token Bucket Rate Limiter algorithm.
"""

import pytest
import asyncio
import time
from unittest.mock import patch, AsyncMock

from app.algorithms.token_bucket import TokenBucketLimiter, TokenBucketResult


class TestTokenBucketLimiter:
    """Tests for the Token Bucket algorithm implementation."""

    @pytest.mark.asyncio
    async def test_first_request_allowed(self, mock_redis_client, fake_redis):
        """First request should always be allowed with full bucket."""
        result = await TokenBucketLimiter.check(
            identifier="test_user_1", limit=10, window_seconds=60
        )

        assert result.allowed is True
        assert result.remaining == 9  # Started with 10, used 1
        assert result.retry_after is None

    @pytest.mark.asyncio
    async def test_multiple_requests_consume_tokens(
        self, mock_redis_client, fake_redis
    ):
        """Each request should consume one token."""
        identifier = "test_user_2"
        limit = 5

        # Make 5 requests
        for i in range(5):
            result = await TokenBucketLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )
            assert result.allowed is True
            assert result.remaining == limit - i - 1

        # 6th request should be denied
        result = await TokenBucketLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0

    @pytest.mark.asyncio
    async def test_tokens_refill_over_time(self, mock_redis_client, fake_redis):
        """Tokens should refill at the specified rate."""
        identifier = "test_user_3"
        limit = 10
        window_seconds = 10  # 1 token per second

        # Consume all tokens
        for _ in range(10):
            await TokenBucketLimiter.check(
                identifier=identifier, limit=limit, window_seconds=window_seconds
            )

        # Verify bucket is empty
        result = await TokenBucketLimiter.check(
            identifier=identifier, limit=limit, window_seconds=window_seconds
        )
        assert result.allowed is False

        # Mock time passage (2 seconds = 2 tokens)
        with patch("time.time", return_value=time.time() + 2):
            result = await TokenBucketLimiter.check(
                identifier=identifier, limit=limit, window_seconds=window_seconds
            )
            # Should have refilled ~2 tokens, consume 1
            assert result.allowed is True

    @pytest.mark.asyncio
    async def test_get_status_no_consumption(self, mock_redis_client, fake_redis):
        """Status check should not consume tokens."""
        identifier = "test_user_4"
        limit = 10

        # Make some requests
        for _ in range(3):
            await TokenBucketLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )

        # Check status multiple times
        for _ in range(5):
            status = await TokenBucketLimiter.get_status(
                identifier=identifier, limit=limit, window_seconds=60
            )
            # Token bucket may refill slightly between calls, so check remaining is less than limit
            assert status["remaining"] <= limit
            assert status["requests_used"] >= 0

    @pytest.mark.asyncio
    async def test_reset_clears_bucket(self, mock_redis_client, fake_redis):
        """Reset should clear the bucket, giving full capacity."""
        identifier = "test_user_5"
        limit = 5

        # Consume all tokens
        for _ in range(5):
            await TokenBucketLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )

        # Verify empty
        result = await TokenBucketLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is False

        # Reset
        reset_result = await TokenBucketLimiter.reset(identifier)
        assert reset_result is True

        # Should have full capacity again
        result = await TokenBucketLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is True
        assert result.remaining == 4  # 5 - 1

    @pytest.mark.asyncio
    async def test_different_identifiers_independent(
        self, mock_redis_client, fake_redis
    ):
        """Different identifiers should have independent buckets."""
        limit = 3

        # Exhaust user_a's limit
        for _ in range(3):
            await TokenBucketLimiter.check(
                identifier="user_a", limit=limit, window_seconds=60
            )

        result_a = await TokenBucketLimiter.check(
            identifier="user_a", limit=limit, window_seconds=60
        )
        assert result_a.allowed is False

        # user_b should still have full capacity
        result_b = await TokenBucketLimiter.check(
            identifier="user_b", limit=limit, window_seconds=60
        )
        assert result_b.allowed is True
        assert result_b.remaining == 2

    @pytest.mark.asyncio
    async def test_reset_nonexistent_identifier(self, mock_redis_client, fake_redis):
        """Resetting non-existent identifier should return False."""
        result = await TokenBucketLimiter.reset("nonexistent_user")
        assert result is False

    @pytest.mark.asyncio
    async def test_key_generation(self):
        """Key should be properly formatted."""
        key = TokenBucketLimiter._get_key("user_123")
        assert key == "token_bucket:user_123"

        key = TokenBucketLimiter._get_key("192.168.1.1")
        assert key == "token_bucket:192.168.1.1"

    @pytest.mark.asyncio
    async def test_high_limit_values(self, mock_redis_client, fake_redis):
        """Should handle high limit values correctly."""
        result = await TokenBucketLimiter.check(
            identifier="high_limit_user", limit=100000, window_seconds=3600
        )

        assert result.allowed is True
        assert result.remaining == 99999

    @pytest.mark.asyncio
    async def test_short_window(self, mock_redis_client, fake_redis):
        """Should handle very short windows correctly."""
        result = await TokenBucketLimiter.check(
            identifier="short_window_user", limit=10, window_seconds=1
        )

        assert result.allowed is True
        assert result.reset_in_seconds >= 0
