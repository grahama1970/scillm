"""Debugger skill adapter — breakpoint-first contract for transport workers."""

from __future__ import annotations

import os
from typing import Any

from ._receipt import base_receipt, sha256_hex


class DebuggerAdapter:
    def invoke(self, spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        args = dict(spec.get("args") or {})
        query = str(args.get("query") or args.get("prompt") or args.get("symptom") or "").strip()
        if not query:
            raise ValueError("debugger skill_call requires args.query, args.prompt, or args.symptom")
        workspace = str(args.get("workspace") or args.get("cwd") or ".")

        if dry_run or os.environ.get("SCILLM_SKILL_DEBUGGER_LIVE", "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            plan_sha = sha256_hex(f"debugger-plan:{query}:{workspace}")
            return base_receipt(
                skill="debugger",
                spec=spec,
                status="ok",
                executor="harness:debugger-adapter",
                artifacts=[{"path": "debugger_plan.md", "sha256": plan_sha}],
                extra={
                    "debugger_contract": {
                        "entrypoint": "breakpoint-first",
                        "workspace": workspace,
                        "required_skills": ["memory", "scillm", "best-practices-scillm"],
                    },
                    "plan_excerpt": (
                        "1. Recall memory for prior scillm lessons\n"
                        "2. Set breakpoints on suspected code path\n"
                        "3. Inspect locals before patching\n"
                        f"Symptom: {query[:300]}"
                    ),
                    "validation": {
                        "useful": True,
                        "why": "dry_run debugger plan (SCILLM_SKILL_DEBUGGER_LIVE=1 for live PTY — not enabled in v1)",
                        "commands_run": ["debugger:plan (dry_run)"],
                    },
                },
                dry_run=True,
            )

        raise NotImplementedError("live debugger execution requires subagent-runner / transport worker")
