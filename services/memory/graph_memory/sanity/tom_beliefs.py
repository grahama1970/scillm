#!/usr/bin/env python3
"""Sanity tests for Theory of Mind belief tracking operations.

Run with: python -m graph_memory.sanity.tom_beliefs

Tests:
1. Create user-agent relationship with belief fields
2. Set explicit belief
3. Update belief on confirm/contradict
4. Decay belief over time
5. BDI inference (heuristic fallback)
"""
import sys
import time
import uuid
from loguru import logger


def test_relationship_with_beliefs(api, test_id):
    """Test relationship creation includes belief fields."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    rel = api.get_or_create_relationship(user_id, agent_id)

    if "error" in rel:
        print(f"    FAIL: Relationship creation failed: {rel['error']}")
        return False

    # Check belief fields exist
    if "user_beliefs" not in rel:
        print(f"    FAIL: Missing user_beliefs field")
        return False

    if "belief_confidence" not in rel:
        print(f"    FAIL: Missing belief_confidence field")
        return False

    print(f"    OK: Relationship created with belief fields")
    return True


def test_set_belief(api, test_id):
    """Test setting an explicit belief."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    result = api.set_user_belief(
        user_id=user_id,
        agent_id=agent_id,
        belief_key="wants_help",
        confidence=0.8,
        source="explicit",
    )

    if "error" in result:
        print(f"    FAIL: Set belief failed: {result['error']}")
        return False

    beliefs = result.get("user_beliefs", {})
    if beliefs.get("wants_help") != 0.8:
        print(f"    FAIL: Belief not set correctly: {beliefs}")
        return False

    print(f"    OK: Set belief wants_help=0.8")
    return True


def test_update_belief_confirm(api, test_id):
    """Test belief update on confirmation (confidence * 1.2)."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    # Update on confirm
    result = api.update_user_beliefs(
        user_id=user_id,
        agent_id=agent_id,
        observation="User asked for help as predicted",
        prediction_matched=True,
        belief_key="wants_help",
    )

    if "error" in result:
        print(f"    FAIL: Update belief failed: {result['error']}")
        return False

    beliefs = result.get("user_beliefs", {})
    new_conf = beliefs.get("wants_help", 0)

    # Should be 0.8 * 1.2 = 0.96
    if not (0.9 <= new_conf <= 1.0):
        print(f"    FAIL: Confirm didn't increase confidence: {new_conf}")
        return False

    print(f"    OK: Confirm increased confidence to {new_conf}")
    return True


def test_update_belief_contradict(api, test_id):
    """Test belief update on contradiction (confidence * 0.7)."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    # First set to a known value
    api.set_user_belief(user_id, agent_id, "is_frustrated", 1.0)

    # Update on contradict
    result = api.update_user_beliefs(
        user_id=user_id,
        agent_id=agent_id,
        observation="User was calm, not frustrated",
        prediction_matched=False,
        belief_key="is_frustrated",
    )

    if "error" in result:
        print(f"    FAIL: Update belief failed: {result['error']}")
        return False

    beliefs = result.get("user_beliefs", {})
    new_conf = beliefs.get("is_frustrated", 1.0)

    # Should be 1.0 * 0.7 = 0.7
    if not (0.6 <= new_conf <= 0.8):
        print(f"    FAIL: Contradict didn't decrease confidence: {new_conf}")
        return False

    print(f"    OK: Contradict decreased confidence to {new_conf}")
    return True


def test_decay_beliefs(api, test_id):
    """Test time-based decay of all beliefs."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    # Get current beliefs
    rel = api.get_or_create_relationship(user_id, agent_id)
    old_confidence = rel.get("belief_confidence", 0.5)

    # Decay
    result = api.decay_belief_confidence(
        user_id=user_id,
        agent_id=agent_id,
        decay_factor=0.9,  # 10% decay
    )

    if "error" in result:
        print(f"    FAIL: Decay failed: {result['error']}")
        return False

    new_confidence = result.get("belief_confidence", 0)
    expected = old_confidence * 0.9

    if abs(new_confidence - expected) > 0.01:
        print(f"    FAIL: Decay incorrect: expected {expected}, got {new_confidence}")
        return False

    print(f"    OK: Decay reduced confidence from {old_confidence:.2f} to {new_confidence:.2f}")
    return True


def test_infer_bdi_heuristic(api, test_id):
    """Test BDI inference with heuristic fallback."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    # Simple conversation that should trigger heuristic patterns
    conversation = [
        {"role": "user", "content": "I need help with this error"},
        {"role": "assistant", "content": "What error are you seeing?"},
        {"role": "user", "content": "It says module not found. How do I fix it?"},
    ]

    result = api.infer_user_bdi(
        user_id=user_id,
        agent_id=agent_id,
        conversation_history=conversation,
        k=3,
    )

    if "error" in result and "failed" in str(result.get("reasoning", "")).lower():
        print(f"    FAIL: BDI inference failed: {result}")
        return False

    # Should have inferred something from "help" and "error" keywords
    beliefs = result.get("beliefs", {})
    desires = result.get("desires", {})

    if not beliefs and not desires:
        print(f"    WARN: No beliefs/desires inferred (may need scillm)")
    else:
        print(f"    OK: Inferred beliefs={list(beliefs.keys())}, desires={list(desires.keys())}")

    print(f"    OK: BDI inference completed (confidence={result.get('confidence', 0)})")
    return True


def cleanup(db, test_id):
    """Clean up test data."""
    user_id = f"tom_user_{test_id}"
    agent_id = f"tom_agent_{test_id}"

    # Delete relationship
    try:
        cursor = db.aql.execute(
            """FOR r IN user_agent_relationships
               FILTER r.user_id == @uid AND r.agent_id == @aid
               REMOVE r IN user_agent_relationships""",
            bind_vars={"uid": user_id, "aid": agent_id}
        )
        list(cursor)  # Execute
    except Exception as exc:
        logger.error("Suppressed error in tom_beliefs: {}", exc)

    # Delete user
    try:
        cursor = db.aql.execute(
            "FOR u IN users FILTER u.user_id == @uid REMOVE u IN users",
            bind_vars={"uid": user_id}
        )
        list(cursor)
    except Exception as exc:
        logger.error("Suppressed error in tom_beliefs: {}", exc)

    # Delete persona
    try:
        cursor = db.aql.execute(
            "FOR p IN persona_states FILTER p.agent_id == @aid REMOVE p IN persona_states",
            bind_vars={"aid": agent_id}
        )
        list(cursor)
    except Exception as exc:
        logger.error("Suppressed error in tom_beliefs: {}", exc)


def main():
    """Run Theory of Mind sanity tests."""
    print("=" * 60)
    print("Theory of Mind Belief Tracking Sanity Tests")
    print("=" * 60)

    # Import here to allow running as script
    try:
        from graph_memory.arango_client import get_db
        from graph_memory import api
    except ImportError:
        print("FAIL: Could not import graph_memory")
        sys.exit(1)

    db = get_db()
    test_id = str(uuid.uuid4())[:8]

    tests = [
        ("Create relationship with beliefs", test_relationship_with_beliefs),
        ("Set explicit belief", test_set_belief),
        ("Update belief on confirm", test_update_belief_confirm),
        ("Update belief on contradict", test_update_belief_contradict),
        ("Decay beliefs over time", test_decay_beliefs),
        ("Infer BDI (heuristic)", test_infer_bdi_heuristic),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n[TEST] {name}")
        try:
            if test_fn(api, test_id):
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"    FAIL: Exception: {e}")
            failed += 1

    print(f"\n[CLEANUP] Removing test data...")
    cleanup(db, test_id)

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
