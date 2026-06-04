"""Memory skill adapter — recall/writeback contract for harness and transport."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ._receipt import base_receipt, sha256_hex


def _memory_base_url() -> str:
    return os.environ.get("SCILLM_MEMORY_HTTP_URL", "http://127.0.0.1:8765").rstrip("/")


class MemoryAdapter:
    def invoke(self, spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        args = dict(spec.get("args") or {})
        query = str(args.get("query") or args.get("prompt") or "").strip()
        if not query:
            raise ValueError("memory skill_call requires args.query or args.prompt")
        limit = int(args.get("limit") or 5)
        collection = str(args.get("collection") or "lessons")

        if dry_run or os.environ.get("SCILLM_SKILL_MEMORY_LIVE", "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            partial_sha = sha256_hex(f"memory-recall:{query}:{collection}")
            return base_receipt(
                skill="memory",
                spec=spec,
                status="ok",
                executor="harness:memory-adapter",
                artifacts=[{"path": "memory_recall.json", "sha256": partial_sha}],
                extra={
                    "memory_contract": {
                        "entrypoint": "/recall",
                        "http_base": _memory_base_url(),
                        "collection": collection,
                        "limit": limit,
                    },
                    "recall_hits": [
                        {
                            "title": "dry-run lesson",
                            "snippet": f"Recall preview for: {query[:120]}",
                            "score": 0.91,
                        }
                    ],
                    "validation": {
                        "useful": True,
                        "why": "dry_run memory recall receipt (set SCILLM_SKILL_MEMORY_LIVE=1 for HTTP recall)",
                        "commands_run": [f"POST {_memory_base_url()}/recall (dry_run)"],
                    },
                },
                dry_run=True,
            )

        payload = json.dumps({"query": query, "collection": collection, "limit": limit}).encode("utf-8")
        req = urllib.request.Request(
            f"{_memory_base_url()}/recall",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=int(spec.get("timeout_sec") or 30)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return base_receipt(
                skill="memory",
                spec=spec,
                status="error",
                executor="harness:memory-adapter",
                errors=[str(exc)],
                extra={"memory_contract": {"http_base": _memory_base_url()}},
                dry_run=False,
            )

        hits = body.get("results") if isinstance(body, dict) else body
        if not isinstance(hits, list):
            hits = []
        artifact_sha = sha256_hex(json.dumps(hits, sort_keys=True, default=str))
        return base_receipt(
            skill="memory",
            spec=spec,
            status="ok",
            executor="harness:memory-adapter",
            artifacts=[{"path": "memory_recall.json", "sha256": artifact_sha}],
            extra={
                "memory_contract": {"http_base": _memory_base_url(), "collection": collection},
                "recall_hits": hits[:limit],
                "validation": {
                    "useful": bool(hits),
                    "why": "live memory HTTP recall",
                    "commands_run": [f"POST {_memory_base_url()}/recall"],
                },
            },
            dry_run=False,
        )
