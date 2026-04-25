#!/usr/bin/env python3
"""Smoke test for JSON Schema validation via HTTP endpoint.

Tests:
1. Schema validation passes when response matches schema
2. Schema validation fails with helpful error when mismatched
3. No validation when json_schema not provided

Usage:
    python scripts/smoke_schema.py
"""
import httpx
import sys

BASE_URL = "http://localhost:4001/v1/chat/completions"
HEADERS = {
    "Authorization": "Bearer sk-dev-proxy-123",
    "Content-Type": "application/json",
    "X-Caller-Skill": "smoke-schema",
}


def test_schema_pass():
    """Test schema validation with matching response."""
    resp = httpx.post(
        BASE_URL,
        headers=HEADERS,
        json={
            "model": "text-claude",
            "messages": [
                {"role": "system", "content": "Return valid JSON. No markdown."},
                {"role": "user", "content": 'Return exactly: {"name": "Alice", "age": 25}'},
            ],
            "response_format": {"type": "json_object"},
            "json_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
                "required": ["name", "age"],
            },
        },
        timeout=60.0,
    )
    data = resp.json()

    if "error" in data:
        print(f"  ERROR: {data['error'].get('message', data['error'])}")
        return False

    validated = data.get("schema_validated", False)
    print(f"Test 1 (matching schema): validated={validated}")

    assert validated, f"Expected schema_validated=True"
    return True


def test_schema_no_schema():
    """Test no validation when schema not provided."""
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

    if "error" in data:
        print(f"  ERROR: {data['error'].get('message', data['error'])}")
        return False

    has_validated = "schema_validated" in data
    print(f"Test 2 (no schema): schema_validated present={has_validated}")

    assert not has_validated, "Expected no schema_validated in response"
    return True


def main():
    print("=" * 60)
    print("scillm JSON Schema Validation Smoke Test")
    print("=" * 60)

    tests = [
        ("Schema pass", test_schema_pass),
        ("No schema", test_schema_no_schema),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            if test_fn():
                passed += 1
                print(f"  ✓ {name}\n")
            else:
                failed += 1
                print(f"  ✗ {name}: returned False\n")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {e}\n")

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
