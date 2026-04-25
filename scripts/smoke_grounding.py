#!/usr/bin/env python3
"""Smoke test for grounding verification via HTTP endpoint.

Tests:
1. Grounding with matching source → verified=true
2. Grounding with non-matching source → verified=false
3. No source → no grounding headers

Usage:
    python scripts/smoke_grounding.py
"""
import httpx
import sys

BASE_URL = "http://localhost:4001/v1/chat/completions"
HEADERS = {
    "Authorization": "Bearer sk-dev-proxy-123",
    "Content-Type": "application/json",
    "X-Caller-Skill": "smoke-grounding",
}


def test_grounding_match():
    """Test grounding with matching content."""
    resp = httpx.post(
        BASE_URL,
        headers=HEADERS,
        json={
            "model": "text-claude",
            "messages": [
                {"role": "system", "content": "Answer using only: The answer is 42."},
                {"role": "user", "content": "What is the answer?"},
            ],
            "source": "The answer is 42.",
            "grounding_threshold": 0.5,
        },
        timeout=60.0,
    )
    data = resp.json()
    score = data.get("grounding_score", 0)
    verified = data.get("grounding_verified", False)

    print(f"Test 1 (matching): score={score:.3f}, verified={verified}")

    # Also check headers
    h_score = resp.headers.get("x-scillm-grounding-score")
    h_verified = resp.headers.get("x-scillm-grounding-verified")
    print(f"  Headers: score={h_score}, verified={h_verified}")

    assert score > 0.3, f"Expected score > 0.3, got {score}"
    assert verified, f"Expected verified=True, got {verified}"
    return True


def test_grounding_mismatch():
    """Test grounding with non-matching content."""
    resp = httpx.post(
        BASE_URL,
        headers=HEADERS,
        json={
            "model": "text-claude",
            "messages": [{"role": "user", "content": "What is 2+2?"}],
            "source": "The sky is blue. Grass is green.",
            "grounding_threshold": 0.9,  # High threshold
        },
        timeout=60.0,
    )
    data = resp.json()
    score = data.get("grounding_score", 1.0)
    verified = data.get("grounding_verified", True)

    print(f"Test 2 (mismatch): score={score:.3f}, verified={verified}")

    assert score < 0.9, f"Expected score < 0.9, got {score}"
    assert not verified, f"Expected verified=False, got {verified}"
    return True


def test_no_grounding():
    """Test no grounding when source not provided."""
    resp = httpx.post(
        BASE_URL,
        headers=HEADERS,
        json={
            "model": "text-claude",
            "messages": [{"role": "user", "content": "Say hello"}],
        },
        timeout=60.0,
    )
    data = resp.json()

    has_score = "grounding_score" in data
    has_header = "x-scillm-grounding-score" in resp.headers

    print(f"Test 3 (no source): body has grounding_score={has_score}, header present={has_header}")

    assert not has_score, f"Expected no grounding_score in body"
    assert not has_header, f"Expected no grounding header"
    return True


def main():
    print("=" * 60)
    print("scillm Grounding Verification Smoke Test")
    print("=" * 60)

    tests = [
        ("Grounding match", test_grounding_match),
        ("Grounding mismatch", test_grounding_mismatch),
        ("No grounding", test_no_grounding),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
            print(f"  ✓ {name}\n")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {e}\n")

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
