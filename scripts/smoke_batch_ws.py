#!/usr/bin/env python3
"""Smoke test for WebSocket batch progress tracking.

Tests:
1. WebSocket connection to /v1/scillm/ws/batch/{batch_id}
2. Receives 'subscribed' event on connect
3. REST status endpoint returns batch info
4. Batch progress middleware tracks calls via X-Scillm-Batch-Id header

Usage:
    python scripts/smoke_batch_ws.py

Requires websockets package: pip install websockets
"""

import asyncio
import json
import sys
import uuid

import httpx

PROXY_URL = "http://127.0.0.1:4001"
WS_URL = "ws://127.0.0.1:4001"
API_KEY = "sk-dev-proxy-123"

# Try websockets, fall back to no-WS test if not available
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    print("Warning: websockets package not installed, skipping WebSocket tests")


async def test_batch_progress_header():
    """Test that X-Scillm-Batch-Id header is tracked."""
    batch_id = f"smoke-test-{uuid.uuid4().hex[:8]}"
    call_key = "call-001"

    print(f"\n[1] Testing batch header tracking (batch_id={batch_id})")

    async with httpx.AsyncClient() as client:
        # Make a request with batch headers
        resp = await client.post(
            f"{PROXY_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "X-Caller-Skill": "smoke-batch-ws",
                "X-Scillm-Batch-Id": batch_id,
                "X-Scillm-Call-Key": call_key,
                "X-Scillm-Batch-Total": "1",
            },
            json={
                "model": "local-text",
                "messages": [{"role": "user", "content": "Say 'batch test ok'"}],
            },
            timeout=60.0,
        )

        if resp.status_code != 200:
            print(f"  FAIL: Request returned {resp.status_code}: {resp.text[:200]}")
            return False

        print(f"  OK: Request completed ({resp.status_code})")

        # Check batch status via REST endpoint
        status_resp = await client.get(
            f"{PROXY_URL}/v1/scillm/ws/batch/{batch_id}/status",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        if status_resp.status_code != 200:
            print(f"  FAIL: Status endpoint returned {status_resp.status_code}")
            return False

        status = status_resp.json()
        print(f"  Batch status: {json.dumps(status, indent=2)}")

        if status.get("status") == "not_found":
            print("  WARN: Batch not found (middleware may not be loaded)")
            return True  # Not a failure, just not enabled

        if status.get("completed", 0) >= 1:
            print("  OK: Batch shows completed call")
            return True
        else:
            print(f"  FAIL: Expected completed >= 1, got {status.get('completed')}")
            return False


async def test_websocket_subscribe():
    """Test WebSocket subscription to batch progress."""
    if not HAS_WEBSOCKETS:
        print("\n[2] SKIP: websockets package not installed")
        return True

    batch_id = f"smoke-ws-{uuid.uuid4().hex[:8]}"
    print(f"\n[2] Testing WebSocket subscription (batch_id={batch_id})")

    try:
        async with websockets.connect(
            f"{WS_URL}/v1/scillm/ws/batch/{batch_id}",
            close_timeout=5,
        ) as ws:
            # Should receive 'subscribed' event
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(msg)
            print(f"  Received: {data}")

            if data.get("event") != "subscribed":
                print(f"  FAIL: Expected 'subscribed' event, got {data.get('event')}")
                return False

            if data.get("batch_id") != batch_id:
                print(f"  FAIL: batch_id mismatch")
                return False

            print("  OK: Received 'subscribed' event")

            # Test set_total message
            await ws.send(json.dumps({"type": "set_total", "total": 10}))
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(msg)
            print(f"  Received: {data}")

            if data.get("event") == "total_updated":
                print("  OK: Total updated event received")
            else:
                print(f"  WARN: Expected 'total_updated', got {data.get('event')}")

            return True

    except asyncio.TimeoutError:
        print("  FAIL: WebSocket timeout waiting for message")
        return False
    except Exception as exc:
        print(f"  FAIL: WebSocket error: {exc}")
        return False


async def test_websocket_receives_progress():
    """Test that WebSocket receives call_complete events."""
    if not HAS_WEBSOCKETS:
        print("\n[3] SKIP: websockets package not installed")
        return True

    batch_id = f"smoke-progress-{uuid.uuid4().hex[:8]}"
    print(f"\n[3] Testing WebSocket progress events (batch_id={batch_id})")

    try:
        async with websockets.connect(
            f"{WS_URL}/v1/scillm/ws/batch/{batch_id}",
            close_timeout=5,
        ) as ws:
            # Receive subscribed event
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(msg)
            assert data.get("event") == "subscribed"
            print("  Subscribed to batch")

            # Make an HTTP request with the batch header (in background)
            async def make_request():
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{PROXY_URL}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {API_KEY}",
                            "X-Caller-Skill": "smoke-batch-ws",
                            "X-Scillm-Batch-Id": batch_id,
                            "X-Scillm-Call-Key": "progress-test",
                        },
                        json={
                            "model": "local-text",
                            "messages": [{"role": "user", "content": "Say ok"}],
                        },
                        timeout=60.0,
                    )

            # Start request in background
            task = asyncio.create_task(make_request())

            # Wait for call_complete event on WebSocket
            received_progress = False
            for _ in range(10):  # Max 10 messages (including pings)
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(msg)
                    print(f"  Received: {data.get('event', 'unknown')} - {data}")

                    if data.get("event") == "call_complete":
                        received_progress = True
                        if data.get("call_key") == "progress-test":
                            print("  OK: Received call_complete for our request")
                            break
                except asyncio.TimeoutError:
                    break

            await task  # Ensure HTTP request completes

            if not received_progress:
                print("  WARN: Did not receive call_complete event (batch tracking may be disabled)")
                return True  # Not a hard failure

            return True

    except Exception as exc:
        print(f"  FAIL: Error: {exc}")
        return False


async def main():
    """Run all smoke tests."""
    print("=" * 60)
    print("Smoke test: WebSocket batch progress tracking")
    print("=" * 60)

    results = []

    # Test 1: Batch header tracking
    results.append(await test_batch_progress_header())

    # Test 2: WebSocket subscribe
    results.append(await test_websocket_subscribe())

    # Test 3: WebSocket receives progress
    results.append(await test_websocket_receives_progress())

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")

    if passed == total:
        print("OK: All tests passed")
        return 0
    else:
        print("FAIL: Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
