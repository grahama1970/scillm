"""Dogpile skill adapter — dry_run for harness gates; live via run.sh when enabled."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

DOGPILE_SCILLM_ENDPOINT = "http://localhost:4001/v1/chat/completions"
DOGPILE_SCILLM_CALLER = "dogpile"
DOGPILE_REASONING_MODEL = "gpt-5.5"
DOGPILE_DEGRADED_SOURCES = ("perplexity",)
DOGPILE_RETRIEVAL_SOURCES = ("brave", "github", "arxiv", "youtube", "wayback")


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_context(args: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(args.get(key) or "")
        for key in ("persona", "rationale", "context")
        if args.get(key)
    }


class DogpileAdapter:
    def invoke(self, spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        args = dict(spec.get("args") or {})
        query = str(args.get("query") or "")
        if not query.strip():
            raise ValueError("dogpile skill_call requires args.query")
        turn_id = str(spec.get("turn_id") or "harness_turns/turn-unknown")
        idempotency_key = str(spec.get("idempotency_key") or f"sha256:{_sha256_hex(query)}")
        started = _iso_now()
        if dry_run:
            report_sha = _sha256_hex(f"dry-run-report:{query}")
            partial_sha = _sha256_hex(f"dry-run-partial:{query}")
            completed = _iso_now()
            return {
                "schema": "memory.skill_invocation.v1",
                "skill": "dogpile",
                "query": query,
                "turn_id": turn_id,
                "thread_id": spec.get("thread_id"),
                "project_scope": spec.get("project_scope"),
                "status": "ok",
                "artifacts": [
                    {"path": "dogpile_partial_results.json", "sha256": partial_sha},
                    {"path": "report.md", "sha256": report_sha},
                ],
                "errors": [],
                "used_for": ["deep_research"],
                "accepted_by_user": None,
                "request_context": _request_context(args),
                "dogpile_contract": {
                    "entrypoint": "./run.sh search",
                    "llm_integration": {
                        "endpoint": DOGPILE_SCILLM_ENDPOINT,
                        "required_header": {"X-Caller-Skill": DOGPILE_SCILLM_CALLER},
                        "model": DOGPILE_REASONING_MODEL,
                        "forbidden_request_fields": ["max_tokens"],
                    },
                    "retrieval_sources": list(DOGPILE_RETRIEVAL_SOURCES),
                    "degraded_sources": [
                        {"provider": source, "status": "skipped_or_degraded"}
                        for source in DOGPILE_DEGRADED_SOURCES
                    ],
                    "incremental_artifact": "dogpile_partial_results.json",
                    "event_prefix": "[dogpile-event]",
                },
                "validation": {
                    "useful": True,
                    "why": "dry_run adapter receipt matches Dogpile runtime contract without launching external retrieval",
                    "commands_run": [f"./run.sh search {query!r} (dry_run)"],
                },
                "latency_sec": 0.1,
                "args": args,
                "created_at": completed,
                "idempotency_key": idempotency_key,
                "started_at": started,
                "completed_at": completed,
                "executor": "harness:dogpile-adapter",
                "exit_code": 0,
                "skill_git_sha": "dry-run",
                "dry_run": True,
            }
        raise NotImplementedError("live dogpile execution is opt-in outside harness gates")
