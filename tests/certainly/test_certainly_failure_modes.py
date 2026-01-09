"""Certainly integration sanity checks - deliberately try to make it fail.

These tests verify that the certainly integration fails gracefully with
clear error messages, not crashes or hangs.

Run with: pytest tests/certainly/test_certainly_failure_modes.py -v
Requires: OPENROUTER_API_KEY set, lean_runner container running
"""
from __future__ import annotations

import asyncio
import os
import pytest
from typing import Any, Dict

# Skip all tests if certainly not available
pytest.importorskip("lean4_prover")

from scillm.integrations.certainly import (
    prove_requirement,
    is_available,
    check_lean_container,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def check_prerequisites():
    """Check that certainly is available and container is running."""
    if not is_available():
        pytest.skip("certainly package not available")
    if not check_lean_container():
        pytest.skip("lean_runner container not running")
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")


# ---------------------------------------------------------------------------
# 1. Empty and malformed requirements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_requirement(check_prerequisites):
    """Empty requirement should fail gracefully."""
    result = await prove_requirement(
        requirement="",
        tactics=[],
        num_candidates=1,
    )
    assert result.get("ok") is False
    # Should have error or diagnosis, not crash
    assert result.get("error") or result.get("diagnosis") or result.get("attempts") is not None


@pytest.mark.asyncio
async def test_whitespace_only_requirement(check_prerequisites):
    """Whitespace-only requirement should fail gracefully."""
    result = await prove_requirement(
        requirement="   \n\t  ",
        tactics=[],
        num_candidates=1,
    )
    assert result.get("ok") is False


@pytest.mark.asyncio
async def test_nonsense_requirement(check_prerequisites):
    """Complete nonsense should fail with diagnosis."""
    result = await prove_requirement(
        requirement="asdfghjkl qwerty zxcvbnm",
        tactics=[],
        num_candidates=1,
    )
    assert result.get("ok") is False
    # Should attempt to diagnose, not crash
    assert "diagnosis" in result or "error" in result or "attempts" in result


# ---------------------------------------------------------------------------
# 2. Mathematically impossible requirements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prove_false(check_prerequisites):
    """Asking to prove something false should fail with diagnosis."""
    result = await prove_requirement(
        requirement="Prove that 1 = 2",
        tactics=[],
        num_candidates=1,
    )
    assert result.get("ok") is False
    # Should not claim success for an impossible proof


@pytest.mark.asyncio
async def test_prove_contradiction(check_prerequisites):
    """Asking to prove a contradiction should fail."""
    result = await prove_requirement(
        requirement="Prove that for all n, n > n",
        tactics=[],
        num_candidates=1,
    )
    assert result.get("ok") is False


@pytest.mark.asyncio
async def test_prove_negation_of_truth(check_prerequisites):
    """Asking to prove negation of a known truth should fail."""
    result = await prove_requirement(
        requirement="Prove that 0 + 0 ≠ 0",
        tactics=[],
        num_candidates=1,
    )
    assert result.get("ok") is False


# ---------------------------------------------------------------------------
# 3. Invalid/nonexistent tactics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nonexistent_tactics(check_prerequisites):
    """Nonexistent tactics should not crash - they're hints, not requirements."""
    result = await prove_requirement(
        requirement="Prove that n + 0 = n for natural numbers",
        tactics=["fake_tactic", "nonexistent", "made_up_tactic"],
        num_candidates=1,
    )
    # Should still attempt proof (tactics are hints)
    # May succeed or fail, but should not crash
    assert "ok" in result
    assert isinstance(result.get("ok"), bool)


@pytest.mark.asyncio
async def test_empty_tactics_list(check_prerequisites):
    """Empty tactics list should work fine."""
    result = await prove_requirement(
        requirement="Prove that 1 + 1 = 2",
        tactics=[],
        num_candidates=1,
    )
    # Should work - tactics are optional
    assert "ok" in result


@pytest.mark.asyncio
async def test_malformed_tactics(check_prerequisites):
    """Malformed tactic strings should not crash."""
    result = await prove_requirement(
        requirement="Prove that n + 0 = n for natural numbers",
        tactics=["", "   ", "tactic;with;semicolons", "tactic\nwith\nnewlines"],
        num_candidates=1,
    )
    assert "ok" in result


# ---------------------------------------------------------------------------
# 4. Ambiguous or underspecified requirements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ambiguous_types(check_prerequisites):
    """Ambiguous type requirement may fail with helpful diagnosis."""
    result = await prove_requirement(
        requirement="Prove that x + y = y + x",  # No type specified
        tactics=[],
        num_candidates=1,
    )
    # May succeed (LLM picks a type) or fail with diagnosis
    assert "ok" in result
    if not result.get("ok"):
        # Should have diagnosis explaining the ambiguity
        assert result.get("diagnosis") or result.get("attempts")


@pytest.mark.asyncio
async def test_vague_requirement(check_prerequisites):
    """Very vague requirement should fail or get lucky."""
    result = await prove_requirement(
        requirement="Prove something about numbers",
        tactics=[],
        num_candidates=1,
    )
    assert "ok" in result  # Should not crash


# ---------------------------------------------------------------------------
# 5. Requirements that conflict with Mathlib (naming conflicts)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mathlib_name_conflict_zero_add(check_prerequisites):
    """Requirement matching Mathlib theorem name should still work (hash suffix)."""
    result = await prove_requirement(
        requirement="Prove that 0 + n = n for natural numbers",
        tactics=["simp"],
        num_candidates=1,
    )
    # Should succeed - hash suffix avoids conflict with Mathlib's zero_add
    if result.get("ok"):
        lean_code = result.get("best", {}).get("lean4", "")
        # Verify theorem has a hash suffix, not bare name
        assert "zero_add" not in lean_code or "_" in lean_code.split("zero_add")[1][:5]


@pytest.mark.asyncio
async def test_mathlib_name_conflict_add_comm(check_prerequisites):
    """Another Mathlib conflict test."""
    result = await prove_requirement(
        requirement="Prove that a + b = b + a for natural numbers",
        tactics=["ring"],
        num_candidates=1,
    )
    # Should not fail due to "add_comm already declared"
    assert "ok" in result
    if not result.get("ok"):
        # If it failed, it shouldn't be due to naming conflict
        for attempt in result.get("attempts", []):
            stderr = attempt.get("stderr", "") + attempt.get("stdout", "")
            assert "already been declared" not in stderr


# ---------------------------------------------------------------------------
# 6. Edge cases in requirement text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unicode_requirement(check_prerequisites):
    """Unicode in requirement should not crash."""
    result = await prove_requirement(
        requirement="Prove that ∀ n : ℕ, n + 0 = n",
        tactics=[],
        num_candidates=1,
    )
    assert "ok" in result


@pytest.mark.asyncio
async def test_very_long_requirement(check_prerequisites):
    """Very long requirement should not hang or crash."""
    long_req = "Prove that " + " and ".join([f"n + {i} = {i} + n" for i in range(50)])
    result = await prove_requirement(
        requirement=long_req,
        tactics=[],
        num_candidates=1,
    )
    assert "ok" in result  # May fail, but should not hang


@pytest.mark.asyncio
async def test_special_characters_requirement(check_prerequisites):
    """Special characters should not cause injection or crashes."""
    result = await prove_requirement(
        requirement='Prove that n = n -- this is a comment\n/- block -/ "string"',
        tactics=[],
        num_candidates=1,
    )
    assert "ok" in result


@pytest.mark.asyncio
async def test_code_injection_attempt(check_prerequisites):
    """Attempt to inject Lean code should not execute arbitrary code."""
    result = await prove_requirement(
        requirement='Prove #eval IO.println "pwned"',
        tactics=[],
        num_candidates=1,
    )
    # Should fail or generate safe code, not execute println
    assert "ok" in result


# ---------------------------------------------------------------------------
# 7. Resource limits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_candidates(check_prerequisites):
    """Zero candidates should fail gracefully."""
    result = await prove_requirement(
        requirement="Prove that 1 = 1",
        tactics=[],
        num_candidates=0,
    )
    # Should fail or clamp to minimum, not crash
    assert "ok" in result or "error" in result


@pytest.mark.asyncio
async def test_negative_candidates(check_prerequisites):
    """Negative candidates should fail gracefully."""
    result = await prove_requirement(
        requirement="Prove that 1 = 1",
        tactics=[],
        num_candidates=-1,
    )
    assert "ok" in result or "error" in result


@pytest.mark.asyncio
async def test_very_short_timeout(check_prerequisites):
    """Very short timeout should fail gracefully, not hang."""
    result = await prove_requirement(
        requirement="Prove a complex theorem about prime numbers",
        tactics=[],
        num_candidates=1,
        compile_timeout_s=1,  # 1 second is too short
    )
    # May succeed or fail, but must not hang
    assert "ok" in result or "error" in result


# ---------------------------------------------------------------------------
# 8. Happy path sanity checks (to verify the system works at all)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_simple_proof_succeeds(check_prerequisites):
    """Basic sanity: a simple proof should succeed."""
    result = await prove_requirement(
        requirement="Prove that for any natural number n, n + 1 > n",
        tactics=["simp", "omega"],
        num_candidates=2,
    )
    assert result.get("ok") is True
    assert result.get("best", {}).get("lean4")
    assert result.get("best", {}).get("judge", {}).get("matches") is True


@pytest.mark.asyncio
async def test_proof_with_valid_tactics(check_prerequisites):
    """Proof with valid tactic hints should work."""
    result = await prove_requirement(
        requirement="Prove that n * 1 = n for natural numbers",
        tactics=["simp", "ring"],
        num_candidates=1,
    )
    # Should succeed
    assert result.get("ok") is True


# ---------------------------------------------------------------------------
# Run standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
