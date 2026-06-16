import asyncio
import time
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app, store

@pytest.fixture
def anyio_backend():
    """Specify the backend for anyio."""
    return "asyncio"

@pytest.fixture(autouse=True)
def reset_store():
    """Clear the idempotency store before each test to ensure test isolation."""
    store.clear()
    store.default_ttl_seconds = 60.0  # Reset default TTL to 60s

@pytest.mark.anyio
async def test_first_payment_success():
    """Verify that the first payment request succeeds with a 2-second delay and X-Cache-Hit: false."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = {"Idempotency-Key": "test-key-1"}
        payload = {"amount": 100, "currency": "GHS"}
        
        start_time = time.time()
        response = await ac.post("/process-payment", json=payload, headers=headers)
        elapsed = time.time() - start_time
        
        assert response.status_code == 200
        assert response.json() == {"message": "Charged 100 GHS"}
        assert response.headers.get("X-Cache-Hit") == "false"
        assert elapsed >= 2.0  # Simulated 2s latency occurred

@pytest.mark.anyio
async def test_duplicate_request_replay():
    """Verify that a duplicate request returns the cached response instantly with X-Cache-Hit: true."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = {"Idempotency-Key": "test-key-2"}
        payload = {"amount": 200, "currency": "GHS"}
        
        # First request
        resp1 = await ac.post("/process-payment", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache-Hit") == "false"
        
        # Second request (duplicate)
        start_time = time.time()
        resp2 = await ac.post("/process-payment", json=payload, headers=headers)
        elapsed = time.time() - start_time
        
        assert resp2.status_code == 200
        assert resp2.json() == resp1.json()
        assert resp2.headers.get("X-Cache-Hit") == "true"
        assert elapsed < 0.1  # Fast cached return

@pytest.mark.anyio
async def test_mismatched_payload_conflict():
    """Verify that reusing the same key with a different body returns 409 Conflict."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = {"Idempotency-Key": "test-key-3"}
        
        # First request (amount: 100)
        resp1 = await ac.post("/process-payment", json={"amount": 100, "currency": "GHS"}, headers=headers)
        assert resp1.status_code == 200
        
        # Second request (amount: 500)
        resp2 = await ac.post("/process-payment", json={"amount": 500, "currency": "GHS"}, headers=headers)
        assert resp2.status_code == 409
        assert resp2.json() == {"detail": "Idempotency key already used for a different request body."}

@pytest.mark.anyio
async def test_missing_idempotency_key():
    """Verify that request fails with 400 Bad Request when Idempotency-Key is missing."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        payload = {"amount": 100, "currency": "GHS"}
        response = await ac.post("/process-payment", json=payload)
        
        assert response.status_code == 400
        assert response.json() == {"detail": "Idempotency-Key header is required"}

@pytest.mark.anyio
async def test_concurrent_requests():
    """
    Verify that multiple concurrent requests with the same key and payload block,
    resulting in exactly one execution, returning the same response for all requests.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = {"Idempotency-Key": "test-key-4"}
        payload = {"amount": 150, "currency": "GHS"}
        
        # Send two requests concurrently
        start_time = time.time()
        results = await asyncio.gather(
            ac.post("/process-payment", json=payload, headers=headers),
            ac.post("/process-payment", json=payload, headers=headers)
        )
        elapsed = time.time() - start_time
        
        # The total time must be ~2s (concurrency worked)
        assert elapsed < 3.0
        assert elapsed >= 2.0
        
        # Check both responses
        resp1, resp2 = results
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        
        # Both must return the same charge body
        assert resp1.json() == {"message": "Charged 150 GHS"}
        assert resp2.json() == {"message": "Charged 150 GHS"}
        
        # One response is the first execution (X-Cache-Hit: false)
        # The other was resolved via Future await (X-Cache-Hit: true)
        cache_hits = [resp1.headers.get("X-Cache-Hit"), resp2.headers.get("X-Cache-Hit")]
        assert "false" in cache_hits
        assert "true" in cache_hits

@pytest.mark.anyio
async def test_ttl_expiration_behavior():
    """Verify that records are evicted after the configured TTL, allowing the request to be processed again."""
    # Set TTL to a small value: 0.5 seconds
    store.default_ttl_seconds = 0.5
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = {"Idempotency-Key": "test-key-5"}
        payload = {"amount": 300, "currency": "GHS"}
        
        # First request
        resp1 = await ac.post("/process-payment", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Cache-Hit") == "false"
        
        # Immediate replay (should hit cache)
        resp2 = await ac.post("/process-payment", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Cache-Hit") == "true"
        
        # Wait for TTL to expire (0.6 seconds)
        await asyncio.sleep(0.6)
        
        # Request again (should be treated as new since cache record has expired)
        start_time = time.time()
        resp3 = await ac.post("/process-payment", json=payload, headers=headers)
        elapsed = time.time() - start_time
        
        assert resp3.status_code == 200
        assert resp3.headers.get("X-Cache-Hit") == "false"
        assert elapsed >= 2.0  # Reprocessed with 2s delay
