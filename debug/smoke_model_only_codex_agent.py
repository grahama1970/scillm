"""
Minimal smoke: only change the model to codex-agent/gpt-5.

Usage
  CODEX_AGENT_API_BASE=http://127.0.0.1:8089 \
  python debug/smoke_model_only_codex_agent.py

Expected
  Prints a strict JSON object (e.g., {"ok": true}) and exits 0.
"""

import os
from scillm import completion


def main() -> None:
    base = os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089")
    r = completion(
        model="codex-agent/gpt-5",  # no custom_llm_provider needed
        messages=[
            {"role": "system", "content": "Return strict JSON: {ok:true}"},
            {"role": "user", "content": "ping"},
        ],
        api_base=base,
        response_format={"type": "json_object"},
        temperature=1,
        max_tokens=50,
        # Reasoning fields are accepted; allowed_openai_params keeps param-first UX consistent
        allowed_openai_params=["reasoning", "reasoning_effort"],
        reasoning={"effort": "medium"},
    )
    print(r.choices[0].message["content"])  # should be a JSON string


if __name__ == "__main__":
    main()

