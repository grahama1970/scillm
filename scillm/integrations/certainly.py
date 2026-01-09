"""Certainly (Lean4 theorem prover) integration for SciLLM.

This module provides direct Python integration with the Certainly package,
avoiding HTTP bridge overhead while maintaining a clean API.

All imports from certainly are LAZY to avoid circular import issues:
- certainly depends on scillm (for LLM calls)
- scillm depends on certainly (for proof orchestration)

Usage:
    from scillm.integrations.certainly import prove_requirement, is_available

    if is_available():
        result = await prove_requirement(
            requirement="Prove that n + 0 = n for natural numbers",
            tactics=["simp"],
        )
        if result["ok"]:
            print(result["best"]["lean4"])

Installation:
    pip install scillm[certainly]
    # or: uv pip install -e ".[certainly]"
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # For type hints only - not imported at runtime
    from lean4_prover.certainly_min import prove_requirement as _prove_requirement_type
    from lean4_prover.core.prover import Prover as _Prover_type


# ---------------------------------------------------------------------------
# Lazy loading machinery
# ---------------------------------------------------------------------------

_certainly_module = None
_prover_class = None
_import_error: Optional[str] = None


def _load_certainly():
    """Lazily load the certainly package. Thread-safe via GIL."""
    global _certainly_module, _prover_class, _import_error

    if _certainly_module is not None:
        return _certainly_module
    if _import_error is not None:
        raise ImportError(_import_error)

    try:
        from lean4_prover import certainly_min
        from lean4_prover.core.prover import Prover

        _certainly_module = certainly_min
        _prover_class = Prover
        return _certainly_module
    except ImportError as e:
        _import_error = (
            f"certainly package not available: {e}\n"
            "Install with: pip install scillm[certainly]\n"
            "Or ensure the lean4 repo is at the expected path."
        )
        raise ImportError(_import_error) from e


def is_available() -> bool:
    """Check if the certainly package is available for import.

    Returns:
        True if certainly can be imported, False otherwise.

    Example:
        >>> from scillm.integrations.certainly import is_available
        >>> if is_available():
        ...     # use certainly features
        ...     pass
    """
    global _import_error
    if _certainly_module is not None:
        return True
    if _import_error is not None:
        return False
    try:
        _load_certainly()
        return True
    except ImportError:
        return False


def require_certainly() -> None:
    """Raise ImportError if certainly is not available.

    Use this at the start of functions that require certainly.
    """
    if not is_available():
        raise ImportError(
            "certainly package required but not available.\n"
            "Install with: pip install scillm[certainly]"
        )


# ---------------------------------------------------------------------------
# Core API - mirrors certainly_min.py but with lazy loading
# ---------------------------------------------------------------------------


async def prove_requirement(
    requirement: str,
    tactics: Optional[List[str]] = None,
    *,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    container: str = "lean_runner",
    compile_timeout_s: Optional[int] = None,
    num_candidates: int = 3,
) -> Dict[str, Any]:
    """Prove a natural language requirement using Lean 4.

    This is the primary entry point for formal verification. It:
    1. Generates Lean 4 proof candidates using DeepSeek Prover
    2. Compiles each candidate in a Docker container
    3. Judges whether compiled proofs match the requirement
    4. Returns the best matching proof or diagnosis

    Args:
        requirement: Natural language description of what to prove.
            Example: "Prove that n + 0 = n for natural numbers"
        tactics: Optional list of Lean tactics to suggest.
            Example: ["simp", "linarith", "ring"]
        model: LLM model to use. Only DeepSeek Prover v2 is supported.
            Default: "openrouter/deepseek/deepseek-prover-v2"
        api_base: API base URL. Default: "https://openrouter.ai/api/v1"
        api_key: API key. Default: $OPENROUTER_API_KEY
        container: Docker container name for Lean compilation.
            Default: "lean_runner"
        compile_timeout_s: Per-compilation timeout in seconds.
        num_candidates: Number of proof candidates to generate (1-5).

    Returns:
        dict with structure:
            On success:
                {"ok": True, "best": {"lean4": "...", "notes": "...", "compile_ms": ...}, "attempts": [...]}
            On failure:
                {"ok": False, "attempts": [...], "diagnosis": {"suggested_requirement_edit": "..."}}
            On error:
                {"ok": False, "error": "..."}

    Example:
        >>> from scillm.integrations.certainly import prove_requirement
        >>> result = await prove_requirement(
        ...     requirement="Prove that 1 + 1 = 2",
        ...     tactics=["simp"],
        ... )
        >>> if result["ok"]:
        ...     print(result["best"]["lean4"])

    Raises:
        ImportError: If certainly package is not installed.

    Note:
        Requires a running "lean_runner" Docker container with Lean 4 + Mathlib.
        Start with: cd /path/to/lean4 && make lean-runner-up
    """
    mod = _load_certainly()
    return await mod.prove_requirement(
        requirement=requirement,
        tactics=tactics or [],
        model=model,
        api_base=api_base,
        api_key=api_key,
        container=container,
        compile_timeout_s=compile_timeout_s,
        num_candidates=num_candidates,
    )


# ---------------------------------------------------------------------------
# Lower-level API - direct access to Prover
# ---------------------------------------------------------------------------


def get_prover(container_name: str = "lean_runner") -> "Prover":
    """Get a Prover instance for direct Lean compilation.

    This provides lower-level access than prove_requirement(), allowing
    you to compile Lean code directly without LLM generation.

    Args:
        container_name: Docker container name. Default: "lean_runner"

    Returns:
        Prover instance for compiling Lean code.

    Example:
        >>> from scillm.integrations.certainly import get_prover
        >>> prover = get_prover()
        >>> if prover.check_docker_container():
        ...     rc, stdout, stderr, feedback = await prover.build_lean(code)

    Raises:
        ImportError: If certainly package is not installed.
    """
    _load_certainly()
    return _prover_class(container_name=container_name)


def check_lean_container(container_name: str = "lean_runner") -> bool:
    """Check if the Lean Docker container is running and healthy.

    Args:
        container_name: Docker container name to check.

    Returns:
        True if container is running and healthy, False otherwise.

    Example:
        >>> from scillm.integrations.certainly import check_lean_container
        >>> if check_lean_container():
        ...     print("Lean container is ready")
        ... else:
        ...     print("Start with: make lean-runner-up")
    """
    try:
        prover = get_prover(container_name)
        return prover.check_docker_container()
    except ImportError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Response normalization (for provider compatibility)
# ---------------------------------------------------------------------------


def normalize_to_model_response(
    result: Dict[str, Any],
    model: str = "certainly/local",
) -> Dict[str, Any]:
    """Normalize a prove_requirement result to LiteLLM ModelResponse shape.

    This formats the result to match what the HTTP bridge returns,
    maintaining backward compatibility with existing code.

    Args:
        result: Result from prove_requirement()
        model: Model name to include in response

    Returns:
        dict matching ModelResponse structure with:
            - choices[0].message.content: summary string
            - additional_kwargs["certainly"]: full result payload
    """
    if result.get("ok"):
        best = result.get("best", {})
        summary = f"Certainly: proved ({best.get('compile_ms', 0)}ms)"
    else:
        error = result.get("error") or result.get("diagnosis", {}).get("diagnosis", "unknown")
        summary = f"Certainly: failed - {error}"

    return {
        "id": f"certainly-{id(result)}",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": summary,
                },
                "finish_reason": "stop",
            }
        ],
        "additional_kwargs": {
            "certainly": result,
            "lean4": result,  # backward compat alias
        },
    }


# ---------------------------------------------------------------------------
# Convenience for common patterns
# ---------------------------------------------------------------------------


async def prove_and_extract(
    requirement: str,
    tactics: Optional[List[str]] = None,
    **kwargs,
) -> tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Prove a requirement and extract the key results.

    Convenience wrapper that returns a simple tuple instead of nested dict.

    Args:
        requirement: Natural language requirement
        tactics: Optional tactic hints
        **kwargs: Passed to prove_requirement()

    Returns:
        (success, lean_code, full_result) tuple where:
            - success: True if proof found
            - lean_code: The Lean 4 code if successful, None otherwise
            - full_result: Complete result dict for inspection

    Example:
        >>> ok, code, result = await prove_and_extract("Prove 1 + 1 = 2")
        >>> if ok:
        ...     print(code)
    """
    result = await prove_requirement(requirement, tactics, **kwargs)
    if result.get("ok"):
        lean_code = result.get("best", {}).get("lean4")
        return True, lean_code, result
    return False, None, result


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def get_certainly_env() -> Dict[str, Optional[str]]:
    """Get certainly-related environment variables.

    Returns:
        dict of relevant environment variable values
    """
    return {
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY"),
        "LEAN4_CONTAINER": os.getenv("LEAN4_CONTAINER", "lean_runner"),
        "CERTAINLY_MODEL": os.getenv("CERTAINLY_MODEL"),
        "CERTAINLY_LLM_DEFAULT": os.getenv("CERTAINLY_LLM_DEFAULT"),
    }


__all__ = [
    # Core API
    "prove_requirement",
    "prove_and_extract",
    # Lower-level access
    "get_prover",
    "check_lean_container",
    # Availability
    "is_available",
    "require_certainly",
    # Utilities
    "normalize_to_model_response",
    "get_certainly_env",
]
