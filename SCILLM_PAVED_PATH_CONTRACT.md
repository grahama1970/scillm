# DevOps SciLLM Paved‑Path Contract (No Hacks / No Wrappers)

This document defines hard rules for how DevOps code in this repository must call SciLLM and Chutes. It exists to prevent regressions back to bespoke wrappers, manual headers, or raw HTTP calls that bypass the paved path.

Scope
- Applies to all code under `experiments/devops/**` (pipelines, workflows, scripts, CLIs, notebooks with executable code).
- Documentation may include `curl` examples for ops visibility, but executable code must follow this contract.

Hard Rules (Do / Don’t)
- DO use SciLLM directly:
  - `from scillm import acompletion, completion`
  - `from scillm.paved import sanity_preflight, list_models_openai_like, chutes_chat_json`
- DO pass credentials via `api_key=`; SciLLM canonicalizes headers for Chutes.
- DO request strict JSON when applicable: `response_format={"type":"json_object"}`.
- DO use paved preflight + discovery:
  - List models: `list_models_openai_like(api_base, api_key)` (Bearer → x‑api‑key fallback handled internally)
  - Preflight: `sanity_preflight(api_base, api_key, model, parallel=3, wall_time_s=30)`
- DO use Router only via SciLLM helpers (never reimplement):
  - `from scillm import Router` or `from scillm.paved import chutes_router_json`

- DON’T set auth headers manually in code (no exceptions):
  - Don’t pass `extra_headers={"Authorization": "Bearer …"}` or `extra_headers={"x-api-key": …}`
  - Don’t hand‑build `requests`/`httpx`/`aiohttp` calls to `/v1/chat/completions` or `/v1/models`
- DON’T implement client‑side alternates/fallbacks for Step 07; preflight must fail fast so operators can fix routing/quota. Use Router flows only where explicitly intended.
- DON’T swallow preflight errors. Surface structured details (`exc_type`, `message`, `status`) to the caller.

Step 07 (Knowledge) Requirements
- Preflight: `sanity_preflight(api_base, api_key, model, parallel=SCILLM_PREFLIGHT_PARALLEL|3, wall_time_s=SCILLM_PREFLIGHT_WALL_S|30)`
- On failure: return `preflight_details` (dict) to the pipeline summary.
- Runtime calls: `scillm.acompletion(..., api_key=CHUTES_API_KEY, custom_llm_provider="openai_like", response_format={"type":"json_object"})`

Allowed Surfaces (CHUTES / OpenAI‑compatible)
- `scillm.acompletion / scillm.completion`
- `scillm.paved.sanity_preflight / list_models_openai_like / chutes_chat_json / chutes_router_json`
- `scillm.Router` (lightweight passthrough; do not wrap)

Enforcement (Grep Guards)
- These patterns must not appear in DevOps code:
  - `extra_headers={.*Authorization.*}` or `extra_headers={.*x-api-key.*}`
  - `requests.(get|post)\(.*chat/completions` or `urllib.request.*chat/completions`
  - Raw `curl … /chat/completions` in executable code (allowed in docs)

Quick Self‑Check
- Allowed example:
  ```python
  from scillm import acompletion
  r = await acompletion(model=os.environ['CHUTES_TEXT_MODEL'],
                        api_base=os.environ['CHUTES_API_BASE'],
                        api_key=os.environ['CHUTES_API_KEY'],
                        custom_llm_provider='openai_like',
                        messages=[{"role":"user","content":"Return only {\\"ok\\":true} as JSON."}],
                        response_format={'type':'json_object'},
                        timeout=30)
  ```
- Disallowed example:
  ```python
  # ❌ manual headers and raw HTTP
  requests.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=payload)
  ```

CI / PR Review Guidance
- If touching DevOps code, reviewers should run:
  - `rg -n "extra_headers=|Authorization|x-api-key|/chat/completions|requests\.(get|post)\(" experiments/devops -g '!**/.venv/**'`
- Reject any occurrence in code (docs are fine) and request migration to the paved helpers above.

Exceptions
- None for CHUTES/SciLLM. If a true exception is required, file a short design note and add a temporary allowlist entry to a local `EXCEPTIONS.md` with an expiration date.

Change History
- 2025‑11‑09: Initial version. Codified paved helpers and strict “no manual headers / no raw HTTP” policy for DevOps.

