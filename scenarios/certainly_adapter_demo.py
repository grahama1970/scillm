#!/usr/bin/env python3
from __future__ import annotations

"""Minimal demo of 'certainly' umbrella provider via adapter routing.

Prereqs:
  export LITELLM_ENABLE_CERTAINLY=1
  export CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787
"""

import os
from litellm import completion


def main() -> int:
    messages = [{"role": "user", "content": "Prove simple lemmas from the list."}]
    items = [
        {"requirement_text": "Nat.add_comm"},
        {"requirement_text": "Nat.add_assoc"},
    ]
    backend = os.getenv("CERTAINLY_BACKEND", "lean4")
    resp = completion(
        model="certainly",
        messages=messages,
        custom_llm_provider="certainly",
        items=items,
        backend=backend,
        session_id="local-demo-session",
        track_id="run-001",
        max_seconds=30,
    )
    print(resp.choices[0].message["content"])  # type: ignore[index]
    ak = getattr(resp, "additional_kwargs", {}) or {}
    print("Has certainly payload:", "certainly" in ak)
    print("Has lean4 payload:", "lean4" in ak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

