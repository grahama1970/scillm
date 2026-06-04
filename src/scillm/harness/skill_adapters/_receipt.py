"""Shared helpers for harness skill adapter receipts."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_receipt(
    *,
    skill: str,
    spec: dict[str, Any],
    status: str,
    executor: str,
    artifacts: list[dict[str, str]] | None = None,
    errors: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    dry_run: bool = False,
    latency_sec: float = 0.1,
) -> dict[str, Any]:
    args = dict(spec.get("args") or {})
    query = str(args.get("query") or args.get("prompt") or "")
    turn_id = str(spec.get("turn_id") or "harness_turns/turn-unknown")
    idempotency_key = str(spec.get("idempotency_key") or f"sha256:{sha256_hex(query)}")
    started = iso_now()
    completed = iso_now()
    receipt: dict[str, Any] = {
        "schema": "memory.skill_invocation.v1",
        "skill": skill,
        "query": query,
        "turn_id": turn_id,
        "thread_id": spec.get("thread_id"),
        "project_scope": spec.get("project_scope"),
        "status": status,
        "artifacts": artifacts or [],
        "errors": errors or [],
        "used_for": [],
        "accepted_by_user": None,
        "request_context": {k: str(args[k]) for k in ("persona", "rationale", "context") if args.get(k)},
        "validation": {
            "useful": status == "ok",
            "why": f"{skill} adapter completed",
            "commands_run": [],
        },
        "latency_sec": latency_sec,
        "args": args,
        "created_at": completed,
        "idempotency_key": idempotency_key,
        "started_at": started,
        "completed_at": completed,
        "executor": executor,
        "exit_code": 0 if status == "ok" else 1,
        "skill_git_sha": "dry-run" if dry_run else "live",
        "dry_run": dry_run,
    }
    if extra:
        receipt.update(extra)
    return receipt
