#!/usr/bin/env python3
"""Sanity tests for CRUD operations and embedding generation.

Run with: python -m graph_memory.sanity.crud_embedding
"""
import sys
import time
import uuid


def test_create(db, test_id):
    """Test CREATE operation."""
    doc = {
        '_key': f'crud_test_{test_id}',
        'title': f'CRUD Test {test_id}',
        'problem': 'Testing create operation',
        'scope': f'sanity_crud_{test_id}',
        'created': int(time.time()),
    }
    result = db.collection('lessons').insert(doc)

    if not result.get('_key'):
        print(f"    FAIL: Create returned no _key")
        return False

    print(f"    OK: Created lesson with key {result['_key']}")
    return True


def test_read(db, test_id):
    """Test READ operation."""
    doc = db.collection('lessons').get(f'crud_test_{test_id}')

    if doc is None:
        print(f"    FAIL: Read returned None")
        return False

    if doc.get('title') != f'CRUD Test {test_id}':
        print(f"    FAIL: Read returned wrong title: {doc.get('title')}")
        return False

    print(f"    OK: Read lesson: {doc['title']}")
    return True


def test_update(db, test_id):
    """Test UPDATE operation."""
    db.collection('lessons').update({
        '_key': f'crud_test_{test_id}',
        'problem': 'UPDATED problem text',
        'updated_at': int(time.time()),
    })

    doc = db.collection('lessons').get(f'crud_test_{test_id}')

    if doc.get('problem') != 'UPDATED problem text':
        print(f"    FAIL: Update did not persist: {doc.get('problem')}")
        return False

    print(f"    OK: Updated lesson problem field")
    return True


def test_upsert(db, test_id):
    """Test UPSERT operation."""
    aql = '''
    UPSERT { _key: @key }
    INSERT { _key: @key, title: @title, scope: @scope, created: @ts }
    UPDATE { title: @title, updated_at: @ts }
    IN lessons
    RETURN NEW
    '''
    result = list(db.aql.execute(aql, bind_vars={
        'key': f'crud_test_{test_id}',
        'title': f'UPSERTED {test_id}',
        'scope': f'sanity_crud_{test_id}',
        'ts': int(time.time()),
    }))

    if not result or result[0].get('title') != f'UPSERTED {test_id}':
        print(f"    FAIL: Upsert did not work correctly")
        return False

    print(f"    OK: Upserted lesson: {result[0]['title']}")
    return True


def test_embedding_generation(db):
    """Test embedding vector generation."""
    from graph_memory.hybrid_search import get_query_embedding

    embedding = get_query_embedding('test query for embedding generation')

    if embedding is None:
        print(f"    FAIL: Embedding generation returned None")
        return False

    if not isinstance(embedding, list):
        print(f"    FAIL: Embedding is not a list: {type(embedding)}")
        return False

    if len(embedding) < 100:
        print(f"    FAIL: Embedding dimension too small: {len(embedding)}")
        return False

    if not all(isinstance(x, float) for x in embedding[:10]):
        print(f"    FAIL: Embedding values are not floats")
        return False

    print(f"    OK: Generated {len(embedding)}-dim embedding, first val: {embedding[0]:.6f}")
    return True


def test_embedding_storage(db, test_id):
    """Test storing and retrieving embeddings."""
    from graph_memory.hybrid_search import get_query_embedding

    if not db.has_collection('lesson_embeddings'):
        print(f"    SKIP: lesson_embeddings collection not found")
        return True

    embedding = get_query_embedding('test embedding storage')
    if not embedding:
        print(f"    FAIL: Could not generate embedding for storage test")
        return False

    # Store
    doc = {
        '_key': f'emb_test_{test_id}',
        'lesson_id': f'lessons/crud_test_{test_id}',
        'embedding': embedding,
        'created': int(time.time()),
    }
    db.collection('lesson_embeddings').insert(doc)

    # Retrieve and verify
    stored = db.collection('lesson_embeddings').get(f'emb_test_{test_id}')

    if stored is None:
        print(f"    FAIL: Could not retrieve stored embedding")
        return False

    if len(stored.get('embedding', [])) != len(embedding):
        print(f"    FAIL: Retrieved embedding has wrong dimension")
        return False

    print(f"    OK: Stored and retrieved {len(embedding)}-dim embedding")
    return True


def test_delete(db, test_id):
    """Test DELETE operation."""
    # Delete lesson
    try:
        db.collection('lessons').delete(f'crud_test_{test_id}')
    except Exception as e:
        print(f"    FAIL: Delete raised exception: {e}")
        return False

    # Verify deletion
    doc = db.collection('lessons').get(f'crud_test_{test_id}')
    if doc is not None:
        print(f"    FAIL: Document still exists after delete")
        return False

    # Delete embedding if exists
    if db.has_collection('lesson_embeddings'):
        try:
            db.collection('lesson_embeddings').delete(f'emb_test_{test_id}')
        except:
            pass

    print(f"    OK: Deleted lesson and verified removal")
    return True


def main():
    print("=== CRUD + Embedding Sanity Tests ===\n")

    from graph_memory.arango_client import get_db
    db = get_db()

    test_id = uuid.uuid4().hex[:8]
    print(f"Using test ID: {test_id}\n")

    tests = [
        ("CREATE", lambda: test_create(db, test_id)),
        ("READ", lambda: test_read(db, test_id)),
        ("UPDATE", lambda: test_update(db, test_id)),
        ("UPSERT", lambda: test_upsert(db, test_id)),
        ("Embedding generation", lambda: test_embedding_generation(db)),
        ("Embedding storage", lambda: test_embedding_storage(db, test_id)),
        ("DELETE", lambda: test_delete(db, test_id)),
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

    # Cleanup any remaining test data
    try:
        for key in list(db.aql.execute(
            'FOR d IN lessons FILTER d._key LIKE "crud_test_%" RETURN d._key'
        )):
            db.collection('lessons').delete(key)
    except:
        pass

    print(f"=== Results: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
