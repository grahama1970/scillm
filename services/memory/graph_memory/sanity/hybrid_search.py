#!/usr/bin/env python3
"""Sanity tests for hybrid search (BM25 + semantic + graph).

These tests insert test data, query it, and verify results.
Run with: python -m graph_memory.sanity.hybrid_search
"""
import sys
import time
import uuid


def setup_test_data(db):
    """Insert test data into collections for search testing."""
    from graph_memory.setup_schema import ensure_collections_and_view
    ensure_collections_and_view()

    test_id = uuid.uuid4().hex[:8]
    test_scope = f"sanity_test_{test_id}"

    # Insert test lesson
    if db.has_collection("lessons"):
        db.collection("lessons").insert({
            "_key": f"test_lesson_{test_id}",
            "title": "Sanity Test Authentication Timeout Fix",
            "problem": "Connection timeout when authenticating to remote server",
            "playbook": "Increase timeout from 10s to 30s",
            "scope": test_scope,
            "created": int(time.time()),
        })

    # Insert test code_symbol
    if db.has_collection("code_symbols"):
        db.collection("code_symbols").insert({
            "_key": f"test_symbol_{test_id}",
            "name": "validate_authentication_token",
            "kind": "function",
            "file_path": "/src/auth/validator.py",
            "line": 42,
            "docstring": "Validates JWT authentication token and returns user info",
            "scope": test_scope,
        })

    # Insert test doc_chunk
    if db.has_collection("doc_chunks"):
        db.collection("doc_chunks").insert({
            "_key": f"test_doc_{test_id}",
            "text": "Authentication configuration requires setting the JWT_SECRET environment variable. Timeout defaults to 30 seconds.",
            "source_file": "/docs/auth.md",
            "scope": test_scope,
        })

    # Insert test lean_theorem
    if db.has_collection("lean_theorems"):
        db.collection("lean_theorems").insert({
            "_key": f"test_theorem_{test_id}",
            "lean_code": "theorem auth_timeout_positive (t : Nat) : t > 0 → valid_timeout t := by simp",
            "notes": "Proves that positive timeout values are valid for authentication",
            "status": "ok",
            "scope": test_scope,
        })

    # Wait for views to index (ArangoSearch is eventually consistent)
    time.sleep(1)

    return test_id, test_scope


def cleanup_test_data(db, test_id, test_scope):
    """Remove test data."""
    for col_name in ["lessons", "code_symbols", "doc_chunks", "lean_theorems"]:
        if db.has_collection(col_name):
            try:
                db.collection(col_name).delete(f"test_lesson_{test_id}")
            except:
                pass
            try:
                db.collection(col_name).delete(f"test_symbol_{test_id}")
            except:
                pass
            try:
                db.collection(col_name).delete(f"test_doc_{test_id}")
            except:
                pass
            try:
                db.collection(col_name).delete(f"test_theorem_{test_id}")
            except:
                pass


def test_bm25_lessons(db, test_scope):
    """Test BM25 search on lessons collection."""
    aql = """
        FOR doc IN lessons_search
        SEARCH ANALYZER(doc.title IN TOKENS(@q, 'text_en'), 'text_en')
        FILTER doc.scope == @scope
        LET score = BM25(doc)
        SORT score DESC
        LIMIT 5
        RETURN {_key: doc._key, title: doc.title, score: score}
    """
    results = list(db.aql.execute(aql, bind_vars={"q": "authentication timeout", "scope": test_scope}))

    if len(results) == 0:
        print(f"    FAIL: No results for 'authentication timeout' in lessons")
        return False

    if results[0]["score"] <= 0:
        print(f"    FAIL: BM25 score is 0 or negative")
        return False

    print(f"    OK: {len(results)} results, top score={results[0]['score']:.3f}")
    return True


def test_bm25_code_symbols(db, test_scope):
    """Test BM25 search on code_symbols collection."""
    views = [v["name"] for v in db.views()]
    if "code_symbols_search" not in views:
        print("    SKIP: code_symbols_search view not found")
        return True  # Skip, not fail

    aql = """
        FOR doc IN code_symbols_search
        SEARCH ANALYZER(
            doc.name IN TOKENS(@q, 'text_en') OR
            doc.docstring IN TOKENS(@q, 'text_en'),
            'text_en'
        )
        FILTER doc.scope == @scope
        LET score = BM25(doc)
        SORT score DESC
        LIMIT 5
        RETURN {_key: doc._key, name: doc.name, score: score}
    """
    results = list(db.aql.execute(aql, bind_vars={"q": "validate authentication token", "scope": test_scope}))

    if len(results) == 0:
        print(f"    FAIL: No results for 'validate authentication token' in code_symbols")
        return False

    print(f"    OK: {len(results)} results, top score={results[0]['score']:.3f}")
    return True


def test_bm25_doc_chunks(db, test_scope):
    """Test BM25 search on doc_chunks collection."""
    views = [v["name"] for v in db.views()]
    if "doc_chunks_search" not in views:
        print("    SKIP: doc_chunks_search view not found")
        return True

    aql = """
        FOR doc IN doc_chunks_search
        SEARCH ANALYZER(doc.text IN TOKENS(@q, 'text_en'), 'text_en')
        FILTER doc.scope == @scope
        LET score = BM25(doc)
        SORT score DESC
        LIMIT 5
        RETURN {_key: doc._key, file: doc.source_file, score: score}
    """
    results = list(db.aql.execute(aql, bind_vars={"q": "authentication configuration JWT", "scope": test_scope}))

    if len(results) == 0:
        print(f"    FAIL: No results for 'authentication configuration JWT' in doc_chunks")
        return False

    print(f"    OK: {len(results)} results, top score={results[0]['score']:.3f}")
    return True


def test_bm25_lean_theorems(db, test_scope):
    """Test BM25 search on lean_theorems collection."""
    views = [v["name"] for v in db.views()]
    if "lean_theorems_search" not in views:
        print("    SKIP: lean_theorems_search view not found")
        return True

    aql = """
        FOR doc IN lean_theorems_search
        SEARCH ANALYZER(
            doc.lean_code IN TOKENS(@q, 'text_en') OR
            doc.notes IN TOKENS(@q, 'text_en'),
            'text_en'
        )
        FILTER doc.status == "ok"
        FILTER doc.scope == @scope
        LET score = BM25(doc)
        SORT score DESC
        LIMIT 5
        RETURN {_key: doc._key, notes: doc.notes, score: score}
    """
    results = list(db.aql.execute(aql, bind_vars={"q": "timeout authentication positive", "scope": test_scope}))

    if len(results) == 0:
        print(f"    FAIL: No results for 'timeout authentication positive' in lean_theorems")
        return False

    print(f"    OK: {len(results)} results, top score={results[0]['score']:.3f}")
    return True


def test_graph_traversal(db):
    """Test multi-hop graph traversal on existing data."""
    if not db.has_collection("lesson_edges"):
        print("    SKIP: lesson_edges collection not found")
        return True

    # Count edges
    edge_count = next(db.aql.execute("RETURN LENGTH(lesson_edges)"), 0)
    if edge_count == 0:
        print("    SKIP: No edges in lesson_edges")
        return True

    # Find a lesson with outbound edges
    aql = """
        FOR e IN lesson_edges
        LIMIT 1
        LET start_doc = DOCUMENT(e._from)
        RETURN start_doc._id
    """
    start_ids = list(db.aql.execute(aql))
    if not start_ids:
        print("    SKIP: No edges to traverse")
        return True

    start_id = start_ids[0]

    # Test 2-hop traversal
    aql = """
        FOR v, e, p IN 1..2 OUTBOUND @start lesson_edges
        LIMIT 10
        RETURN {to: v.title, depth: LENGTH(p.edges)}
    """
    results = list(db.aql.execute(aql, bind_vars={"start": start_id}))

    if len(results) == 0:
        print(f"    FAIL: No traversal results from {start_id}")
        return False

    print(f"    OK: {len(results)} paths from {start_id}")
    return True


def test_unified_query(db, test_scope):
    """Test unified query via MemoryClient."""
    from graph_memory import MemoryClient

    client = MemoryClient(scope=test_scope)
    result = client.query("authentication timeout", k=10)

    items = result.get("items", [])
    errors = result.get("errors", [])

    if errors:
        print(f"    WARN: Errors during query: {errors}")

    if len(items) == 0:
        print(f"    FAIL: No results from unified query")
        return False

    sources = set(item.get("_source") for item in items)
    print(f"    OK: {len(items)} results from sources: {sources}")
    return True


def test_hybrid_search_module(db, test_scope):
    """Test hybrid_search module directly."""
    from graph_memory.hybrid_search import unified_hybrid_search, hybrid_search_lessons

    # Test unified hybrid search
    result = unified_hybrid_search(db, "authentication timeout", scope=test_scope, k=10)

    items = result.get("items", [])
    errors = result.get("errors", [])

    if errors:
        print(f"    WARN: Errors during hybrid search: {errors}")

    if len(items) == 0:
        print(f"    FAIL: No results from unified_hybrid_search")
        return False

    # Check that items have scores
    for item in items:
        if "score" not in item:
            print(f"    FAIL: Item missing score: {item.get('_key')}")
            return False

    # Check source attribution
    sources = set(item.get("_source") for item in items)
    print(f"    OK: {len(items)} results, sources: {sources}, top score: {items[0].get('score', 0):.3f}")
    return True


def main():
    print("=== Hybrid Search Sanity Tests ===\n")

    from graph_memory.arango_client import get_db
    db = get_db()

    # Setup test data
    print("Setting up test data...")
    test_id, test_scope = setup_test_data(db)
    print(f"  Created test data with scope: {test_scope}\n")

    tests = [
        ("BM25 lessons", lambda: test_bm25_lessons(db, test_scope)),
        ("BM25 code_symbols", lambda: test_bm25_code_symbols(db, test_scope)),
        ("BM25 doc_chunks", lambda: test_bm25_doc_chunks(db, test_scope)),
        ("BM25 lean_theorems", lambda: test_bm25_lean_theorems(db, test_scope)),
        ("Graph traversal", lambda: test_graph_traversal(db)),
        ("Unified query", lambda: test_unified_query(db, test_scope)),
        ("Hybrid search module", lambda: test_hybrid_search_module(db, test_scope)),
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

    # Cleanup
    print("Cleaning up test data...")
    cleanup_test_data(db, test_id, test_scope)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
