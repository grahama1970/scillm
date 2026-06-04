"""Dispatch scillm.skill_call.v1 to registered skill adapters.

The dispatcher validates that ``timeout_sec`` is present and well-formed. Wall-clock
timeout enforcement is adapter-owned because adapters may launch different local
runtimes, subprocesses, or remote calls.
"""

from __future__ import annotations

from typing import Any

from .debugger import DebuggerAdapter
from .dogpile import DogpileAdapter
from .memory import MemoryAdapter
from .project_knowledge import ProjectKnowledgeAdapter
from .scillm import ScillmAdapter
from .test_interactions import TestInteractionsAdapter

SKILL_CALL_SCHEMA = "scillm.skill_call.v1"
SKILL_CALL_ACTION = "skill_call"

_ADAPTERS = {
    "dogpile": DogpileAdapter(),
    "memory": MemoryAdapter(),
    "project-knowledge": ProjectKnowledgeAdapter(),
    "scillm": ScillmAdapter(),
    "test-interactions": TestInteractionsAdapter(),
    "debugger": DebuggerAdapter(),
}


class SkillAdapterError(RuntimeError):
    pass


class SkillCallContractError(SkillAdapterError):
    pass


def _require_non_empty_str(spec: dict[str, Any], field: str) -> str:
    value = spec.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SkillCallContractError(f"skill_call requires non-empty {field}")
    return value


def _validate_skill_call_spec(spec: dict[str, Any]) -> str:
    if not isinstance(spec, dict):
        raise SkillCallContractError("skill_call spec must be an object")
    if spec.get("schema") != SKILL_CALL_SCHEMA:
        raise SkillCallContractError(f"skill_call schema must be {SKILL_CALL_SCHEMA}")
    if spec.get("action") != SKILL_CALL_ACTION:
        raise SkillCallContractError(f"skill_call action must be {SKILL_CALL_ACTION}")

    skill = _require_non_empty_str(spec, "skill")
    _require_non_empty_str(spec, "skill_call_id")
    _require_non_empty_str(spec, "idempotency_key")
    _require_non_empty_str(spec, "requested_by")
    _require_non_empty_str(spec, "turn_id")

    args = spec.get("args")
    if not isinstance(args, dict):
        raise SkillCallContractError("skill_call args must be an object")

    timeout = spec.get("timeout_sec")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise SkillCallContractError("skill_call timeout_sec must be a positive integer")

    allowed_tools = spec.get("allowed_tools")
    if not isinstance(allowed_tools, list) or not allowed_tools:
        raise SkillCallContractError("skill_call allowed_tools must be a non-empty list")
    if any(not isinstance(tool, str) or not tool.strip() for tool in allowed_tools):
        raise SkillCallContractError("skill_call allowed_tools entries must be non-empty strings")

    allowed_tool = f"{skill}.run_sh"
    if allowed_tools != [allowed_tool]:
        raise SkillCallContractError(f"skill_call allowed_tools must be exactly [{allowed_tool!r}]")
    return skill


def run_skill_call(spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    skill = _validate_skill_call_spec(spec)
    adapter = _ADAPTERS.get(skill)
    if adapter is None:
        raise SkillAdapterError(f"no adapter registered for skill {skill!r}")
    return adapter.invoke(spec, dry_run=dry_run)
