#!/usr/bin/env python3
from __future__ import annotations
import os, json
def main() -> None:
    os.environ.setdefault("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089")
    os.environ.setdefault("LITELLM_ENABLE_CODEX_AGENT", "1")
    # Import after env so provider registers
    from scillm import completion
    resp = completion(
        model="codex-agent/gpt-5",
        custom_llm_provider="codex-agent",
        messages=[
            {"role":"system","content":"Return STRICT JSON: {best_id:string, rationale_short:string}."},
            {"role":"user","content":json.dumps({"pair":"A vs B","A":"safer, clearer","B":"riskier, faster"})}
        ],
        reasoning_effort="high",
        response_format={"type":"json_object"},
        temperature=1,
        max_tokens=256,
        allowed_openai_params=["reasoning_effort","reasoning"],
        api_base=os.environ["CODEX_AGENT_API_BASE"],
    )
    print(resp.choices[0].message["content"])  # type: ignore[index]

if __name__ == "__main__":
    main()
