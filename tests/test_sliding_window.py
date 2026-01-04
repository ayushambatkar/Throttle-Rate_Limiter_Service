"""
Unit tests for Sliding Window Log Rate Limiter algorithm.
"""

import pytest
import asyncio
import time
from unittest.mock import patch, AsyncMock

from app.algorithms.sliding_window import SlidingWindowLimiter, SlidingWindowResult


class TestSlidingWindowLimiter:
    """Tests for the Sliding Window Log algorithm implementation."""

    @pytest.mark.asyncio
    async def test_first_request_allowed(self, mock_redis_client, fake_redis):
        """First request should always be allowed."""
        result = await SlidingWindowLimiter.check(
            identifier="sw_test_user_1", limit=10, window_seconds=60
        )

        assert result.allowed is True
        assert result.remaining == 9  # limit - 1
        assert result.retry_after is None

    @pytest.mark.asyncio
    async def test_requests_within_limit(self, mock_redis_client, fake_redis):
        """All requests within limit should be allowed."""
        identifier = "sw_test_user_2"
        limit = 5

        for i in range(5):
            result = await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )
            assert result.allowed is True
            assert result.remaining == limit - i - 1

    @pytest.mark.asyncio
    async def test_requests_exceed_limit(self, mock_redis_client, fake_redis):
        """Requests exceeding limit should be denied."""
        identifier = "sw_test_user_3"
        limit = 3

        # Make limit requests
        for _ in range(3):
            result = await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )
            assert result.allowed is True

        # Next request should be denied
        result = await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0

    @pytest.mark.asyncio
    async def test_window_expiration(self, mock_redis_client, fake_redis):
        """Old requests should expire outside the window."""
        identifier = "sw_test_user_4"
        limit = 2
        window_seconds = 5

        # Make 2 requests to exhaust limit
        for _ in range(2):
            await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=window_seconds
            )

        # Verify limit reached
        result = await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=window_seconds
        )
        assert result.allowed is False

        # Simulate time passing beyond window
        with patch("time.time", return_value=time.time() + window_seconds + 1):
            result = await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=window_seconds
            )
            # Old requests should have expired
            assert result.allowed is True

    @pytest.mark.asyncio
    async def test_get_status_no_consumption(self, mock_redis_client, fake_redis):
        """Status check should not count as a request."""
        identifier = "sw_test_user_5"
        limit = 10

        # Make 3 requests
        for _ in range(3):
            await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )

        # Check status multiple times - should not change
        for _ in range(5):
            status = await SlidingWindowLimiter.get_status(
                identifier=identifier, limit=limit, window_seconds=60
            )
            assert status["requests_used"] == 3
            assert status["remaining"] == 7

    @pytest.mark.asyncio
    async def test_reset_clears_window(self, mock_redis_client, fake_redis):
        """Reset should clear all request history."""
        identifier = "sw_test_user_6"
        limit = 3

        # Exhaust limit
        for _ in range(3):
            await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )

        # Verify exhausted
        result = await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is False

        # Reset
        reset_result = await SlidingWindowLimiter.reset(identifier)
        assert reset_result is True

        # Should be allowed again
        result = await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is True
        assert result.remaining == 2

    @pytest.mark.asyncio
    async def test_different_identifiers_independent(
        self, mock_redis_client, fake_redis
    ):
        """Different identifiers should have independent windows."""
        limit = 2

        # Exhaust user_x's limit
        for _ in range(2):
            await SlidingWindowLimiter.check(
                identifier="user_x", limit=limit, window_seconds=60
            )

        result_x = await SlidingWindowLimiter.check(
            identifier="user_x", limit=limit, window_seconds=60
        )
        assert result_x.allowed is False

        # user_y should have full capacity
        result_y = await SlidingWindowLimiter.check(
            identifier="user_y", limit=limit, window_seconds=60
        )
        assert result_y.allowed is True
        assert result_y.remaining == 1

    @pytest.mark.asyncio
    async def test_reset_nonexistent_identifier(self, mock_redis_client, fake_redis):
        """Resetting non-existent identifier should return False."""
        result = await SlidingWindowLimiter.reset("sw_nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_key_generation(self):
        """Key should be properly formatted."""
        key = SlidingWindowLimiter._get_key("user_456")
        assert key == "sliding_window:user_456"

        key = SlidingWindowLimiter._get_key("10.0.0.1")
        assert key == "sliding_window:10.0.0.1"

    @pytest.mark.asyncio
    async def test_retry_after_calculation(self, mock_redis_client, fake_redis):
        """Retry-after should indicate when oldest request expires."""
        identifier = "sw_retry_user"
        limit = 2
        window_seconds = 60

        # Make 2 requests
        await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=window_seconds
        )
        await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=window_seconds
        )

        # Next request should be denied with retry_after
        result = await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=window_seconds
        )

        assert result.allowed is False
        assert result.retry_after is not None
        # retry_after should be close to window_seconds (oldest request just happened)
        assert 0 < result.retry_after <= window_seconds

    @pytest.mark.asyncio
    async def test_high_volume_requests(self, mock_redis_client, fake_redis):
        """Should handle high volume of requests correctly."""
        identifier = "sw_high_volume"
        limit = 100

        # Make 100 requests
        for i in range(100):
            result = await SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )
            assert result.allowed is True
            assert result.remaining == limit - i - 1

        # 101st should fail
        result = await SlidingWindowLimiter.check(
            identifier=identifier, limit=limit, window_seconds=60
        )
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, mock_redis_client, fake_redis):
        """Should handle concurrent requests atomically."""
        identifier = "sw_concurrent"
        limit = 10

        # Make concurrent requests
        tasks = [
            SlidingWindowLimiter.check(
                identifier=identifier, limit=limit, window_seconds=60
            )
            for _ in range(15)
        ]

        results = await asyncio.gather(*tasks)

        # Exactly 10 should be allowed
        allowed_count = sum(1 for r in results if r.allowed)
        denied_count = sum(1 for r in results if not r.allowed)

        assert allowed_count == 10
        assert denied_count == 5
