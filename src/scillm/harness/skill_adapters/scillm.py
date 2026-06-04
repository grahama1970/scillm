"""Scillm skill adapter — institutional LLM contract via localhost proxy."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ._receipt import base_receipt, sha256_hex

SCILLM_ENDPOINT = os.environ.get("SCILLM_PROXY_URL", "http://127.0.0.1:4001").rstrip("/")
SCILLM_CHAT_PATH = "/v1/chat/completions"


class ScillmAdapter:
    def invoke(self, spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        args = dict(spec.get("args") or {})
        query = str(args.get("query") or args.get("prompt") or "").strip()
        if not query:
            raise ValueError("scillm skill_call requires args.query or args.prompt")
        model = str(args.get("model") or "gpt-5.5")
        caller = str(args.get("caller_skill") or spec.get("requested_by") or "scillm")
        transport_run_id = str(args.get("transport_run_id") or "")

        if dry_run or os.environ.get("SCILLM_SKILL_SCILLM_LIVE", "1").strip().lower() in {
            "0",
            "false",
            "no",
        }:
            response_sha = sha256_hex(f"scillm-response:{query}:{model}")
            return base_receipt(
                skill="scillm",
                spec=spec,
                status="ok",
                executor="harness:scillm-adapter",
                artifacts=[{"path": "scillm_response.json", "sha256": response_sha}],
                extra={
                    "scillm_contract": {
                        "endpoint": f"{SCILLM_ENDPOINT}{SCILLM_CHAT_PATH}",
                        "required_header": {"X-Caller-Skill": caller},
                        "model": model,
                        "forbidden_request_fields": ["max_tokens"],
                    },
                    "assistant_excerpt": f"(dry_run) scillm would answer: {query[:200]}",
                    "validation": {
                        "useful": True,
                        "why": "dry_run scillm receipt (SCILLM_SKILL_SCILLM_LIVE=0)",
                        "commands_run": [f"POST {SCILLM_ENDPOINT}{SCILLM_CHAT_PATH} (dry_run)"],
                    },
                },
                dry_run=True,
            )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": query}],
            "stream": False,
        }
        token = os.environ.get("SCILLM_PROXY_TOKEN", os.environ.get("OPENAI_API_KEY", "sk-dev-proxy-123"))
        req = urllib.request.Request(
            f"{SCILLM_ENDPOINT}{SCILLM_CHAT_PATH}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Caller-Skill": caller,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=int(spec.get("timeout_sec") or 120)) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
            return base_receipt(
                skill="scillm",
                spec=spec,
                status="error",
                executor="harness:scillm-adapter",
                errors=[str(exc)],
                extra={"scillm_contract": {"endpoint": f"{SCILLM_ENDPOINT}{SCILLM_CHAT_PATH}"}},
                dry_run=False,
            )

        excerpt = ""
        choices = body.get("choices") if isinstance(body, dict) else None
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else {}
            if isinstance(message, dict):
                excerpt = str(message.get("content") or "")[:4000]
        artifact_sha = sha256_hex(json.dumps(body, sort_keys=True, default=str)[:8000])
        return base_receipt(
            skill="scillm",
            spec=spec,
            status="ok",
            executor="harness:scillm-adapter",
            artifacts=[{"path": "scillm_response.json", "sha256": artifact_sha}],
            extra={
                "scillm_contract": {
                    "endpoint": f"{SCILLM_ENDPOINT}{SCILLM_CHAT_PATH}",
                    "model": model,
                    "transport_run_id": transport_run_id,
                },
                "assistant_excerpt": excerpt,
                "validation": {
                    "useful": bool(excerpt.strip()),
                    "why": "live scillm proxy completion",
                    "commands_run": [f"POST {SCILLM_ENDPOINT}{SCILLM_CHAT_PATH}"],
                },
            },
            dry_run=False,
        )
