#!/usr/bin/env python3
"""Sanity tests for error handling in search operations.

Run with: python -m graph_memory.sanity.error_handling
"""
import sys


def test_bm25_nonexistent_view(db):
    """Test that BM25 search on non-existent view raises proper error."""
    from graph_memory.hybrid_search import bm25_search_collection

    try:
        bm25_search_collection(
            db,
            'nonexistent_view_xyz',
            'test query',
            '',
            5,
            search_fields=['text'],
            return_fields={'doc._key': '_key'},
        )
        print("    FAIL: No exception raised for nonexistent view")
        return False
    except Exception as e:
        error_type = type(e).__name__
        if 'AQL' in error_type or 'Error' in error_type:
            print(f"    OK: Proper exception raised: {error_type}")
            return True
        print(f"    FAIL: Unexpected exception type: {error_type}")
        return False


def test_unified_search_graceful_degradation(db):
    """Test that unified search doesn't crash on errors."""
    from graph_memory.hybrid_search import unified_hybrid_search

    result = unified_hybrid_search(db, 'test query', k=5)

    if 'errors' not in result:
        print("    FAIL: Result missing 'errors' key")
        return False

    if 'items' not in result:
        print("    FAIL: Result missing 'items' key")
        return False

    if 'meta' not in result:
        print("    FAIL: Result missing 'meta' key")
        return False

    print(f"    OK: Result has all required keys (meta, items, errors)")
    return True


def test_unified_search_partial_failure(db):
    """Test that unified search returns partial results if some sources fail."""
    from graph_memory.hybrid_search import unified_hybrid_search

    # Search with sources='all' - even if some fail, others should work
    result = unified_hybrid_search(db, 'authentication', k=10, sources='all')

    items = result.get('items', [])
    errors = result.get('errors', [])

    # Should have some results from at least lessons
    if len(items) == 0 and len(errors) == 0:
        print("    FAIL: No results and no errors - something is wrong")
        return False

    sources_found = set(item.get('_source') for item in items)
    print(f"    OK: Got {len(items)} items from {sources_found}, {len(errors)} errors")
    return True


def test_embedding_failure_handling(db):
    """Test that embedding failures don't crash search."""
    from graph_memory.hybrid_search import get_query_embedding

    # Empty query should handle gracefully
    result = get_query_embedding('')

    # Should return None or empty, not crash
    if result is None or (isinstance(result, list) and len(result) == 0):
        print("    OK: Empty query handled gracefully (returns None/empty)")
        return True

    # If it returns something, that's also OK as long as it didn't crash
    print(f"    OK: Empty query returned {type(result).__name__} (didn't crash)")
    return True


def test_invalid_scope_handling(db):
    """Test that invalid scope doesn't crash search."""
    from graph_memory.hybrid_search import unified_hybrid_search

    result = unified_hybrid_search(
        db,
        'test query',
        scope='nonexistent_scope_xyz_12345',
        k=5
    )

    # Should return empty results, not crash
    if 'items' not in result:
        print("    FAIL: Missing items key")
        return False

    # Empty results for nonexistent scope is expected
    print(f"    OK: Invalid scope returned {len(result.get('items', []))} items (no crash)")
    return True


def test_aql_injection_prevention(db):
    """Test that special characters in query don't cause AQL injection."""
    from graph_memory.hybrid_search import unified_hybrid_search

    # Try queries with potentially dangerous characters
    dangerous_queries = [
        "test'; DROP lessons; --",
        "test\" OR 1=1 --",
        "test\nRETURN 1",
        "test') OR ('1'='1",
    ]

    for query in dangerous_queries:
        try:
            result = unified_hybrid_search(db, query, k=5)
            if 'errors' not in result:
                print(f"    FAIL: Dangerous query didn't return proper structure")
                return False
        except Exception as e:
            print(f"    FAIL: Dangerous query caused exception: {e}")
            return False

    print(f"    OK: All {len(dangerous_queries)} dangerous queries handled safely")
    return True


def main():
    print("=== Error Handling Sanity Tests ===\n")

    from graph_memory.arango_client import get_db
    db = get_db()

    tests = [
        ("BM25 nonexistent view", lambda: test_bm25_nonexistent_view(db)),
        ("Unified search graceful degradation", lambda: test_unified_search_graceful_degradation(db)),
        ("Unified search partial failure", lambda: test_unified_search_partial_failure(db)),
        ("Embedding failure handling", lambda: test_embedding_failure_handling(db)),
        ("Invalid scope handling", lambda: test_invalid_scope_handling(db)),
        ("AQL injection prevention", lambda: test_aql_injection_prevention(db)),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"Testing {name}...")
        try:
            if test_fn():
                print(f"  PASS: {name}\n")
                passed += 1
            else:
                print(f"  FAIL: {name}\n")
                failed += 1
        except Exception as e:
            print(f"  ERROR: {name}: {e}\n")
            failed += 1

    print(f"=== Results: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
