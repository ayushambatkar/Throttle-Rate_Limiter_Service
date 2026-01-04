"""
Integration tests for the Rate Limiter API endpoints.
"""

import pytest


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, test_client):
        """Health check should return healthy when Redis is connected."""
        response = await test_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["redis_connected"] is True
        assert "version" in data


class TestRateLimitCheckEndpoint:
    """Tests for the POST /rate-limit/check endpoint."""

    @pytest.mark.asyncio
    async def test_check_token_bucket_allowed(self, test_client):
        """Token bucket check should allow first request."""
        response = await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": "api_user_1",
                "algorithm": "token_bucket",
                "limit": 100,
                "window_seconds": 60,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is True
        assert data["remaining"] == 99
        assert data["retry_after"] is None

    @pytest.mark.asyncio
    async def test_check_sliding_window_allowed(self, test_client):
        """Sliding window check should allow first request."""
        response = await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": "api_user_2",
                "algorithm": "sliding_window",
                "limit": 100,
                "window_seconds": 60,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is True
        assert data["remaining"] == 99

    @pytest.mark.asyncio
    async def test_check_rate_limit_exceeded(self, test_client):
        """Should return denied when rate limit exceeded."""
        identifier = "api_rate_limited_user"

        # Exhaust the limit
        for _ in range(5):
            await test_client.post(
                "/rate-limit/check",
                json={
                    "identifier": identifier,
                    "algorithm": "token_bucket",
                    "limit": 5,
                    "window_seconds": 60,
                },
            )

        # Next request should be denied
        response = await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": identifier,
                "algorithm": "token_bucket",
                "limit": 5,
                "window_seconds": 60,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is False
        assert data["remaining"] == 0
        assert data["retry_after"] is not None

    @pytest.mark.asyncio
    async def test_check_invalid_algorithm(self, test_client):
        """Should return error for invalid algorithm."""
        response = await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": "test_user",
                "algorithm": "invalid_algo",
                "limit": 100,
                "window_seconds": 60,
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_check_invalid_limit(self, test_client):
        """Should return error for invalid limit."""
        response = await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": "test_user",
                "algorithm": "token_bucket",
                "limit": 0,  # Invalid
                "window_seconds": 60,
            },
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_check_default_values(self, test_client):
        """Should use default values when not provided."""
        response = await test_client.post(
            "/rate-limit/check", json={"identifier": "default_test_user"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is True


class TestRateLimitStatusEndpoint:
    """Tests for the GET /rate-limit/status/{identifier} endpoint."""

    @pytest.mark.asyncio
    async def test_get_status_new_user(self, test_client):
        """Status for new user should show no usage."""
        response = await test_client.get(
            "/rate-limit/status/new_status_user",
            params={"algorithm": "token_bucket", "limit": 100, "window_seconds": 60},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["identifier"] == "new_status_user"
        assert data["requests_used"] == 0
        assert data["limit"] == 100
        assert data["algorithm"] == "token_bucket"

    @pytest.mark.asyncio
    async def test_get_status_after_requests(self, test_client):
        """Status should reflect request usage."""
        identifier = "status_user_with_requests"

        # Make some requests
        for _ in range(3):
            await test_client.post(
                "/rate-limit/check",
                json={
                    "identifier": identifier,
                    "algorithm": "token_bucket",
                    "limit": 10,
                    "window_seconds": 60,
                },
            )

        # Check status
        response = await test_client.get(
            f"/rate-limit/status/{identifier}",
            params={"algorithm": "token_bucket", "limit": 10, "window_seconds": 60},
        )

        assert response.status_code == 200
        data = response.json()
        # Token bucket may refill slightly between calls
        assert data["requests_used"] >= 2


class TestRateLimitResetEndpoint:
    """Tests for the DELETE /rate-limit/reset/{identifier} endpoint."""

    @pytest.mark.asyncio
    async def test_reset_existing_user(self, test_client):
        """Reset should clear rate limit data."""
        identifier = "reset_test_user"

        # Make some requests
        for _ in range(5):
            await test_client.post(
                "/rate-limit/check",
                json={
                    "identifier": identifier,
                    "algorithm": "token_bucket",
                    "limit": 10,
                    "window_seconds": 60,
                },
            )

        # Reset
        response = await test_client.delete(f"/rate-limit/reset/{identifier}")

        assert response.status_code == 200
        data = response.json()
        assert identifier in data["message"]

        # Verify reset worked
        check_response = await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": identifier,
                "algorithm": "token_bucket",
                "limit": 10,
                "window_seconds": 60,
            },
        )

        check_data = check_response.json()
        assert check_data["remaining"] == 9  # Full capacity minus 1

    @pytest.mark.asyncio
    async def test_reset_specific_algorithm(self, test_client):
        """Reset with specific algorithm should only reset that algorithm."""
        identifier = "reset_algo_user"

        # Make requests with both algorithms
        await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": identifier,
                "algorithm": "token_bucket",
                "limit": 10,
                "window_seconds": 60,
            },
        )
        await test_client.post(
            "/rate-limit/check",
            json={
                "identifier": identifier,
                "algorithm": "sliding_window",
                "limit": 10,
                "window_seconds": 60,
            },
        )

        # Reset only token bucket
        response = await test_client.delete(
            f"/rate-limit/reset/{identifier}", params={"algorithm": "token_bucket"}
        )

        assert response.status_code == 200


class TestAPIDocumentation:
    """Tests for API documentation endpoints."""

    @pytest.mark.asyncio
    async def test_openapi_available(self, test_client):
        """OpenAPI spec should be available."""
        response = await test_client.get("/openapi.json")

        assert response.status_code == 200
        data = response.json()
        assert "openapi" in data
        assert "paths" in data

    @pytest.mark.asyncio
    async def test_swagger_ui_available(self, test_client):
        """Swagger UI should be available."""
        response = await test_client.get("/docs")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_redoc_available(self, test_client):
        """ReDoc should be available."""
        response = await test_client.get("/redoc")

        assert response.status_code == 200
