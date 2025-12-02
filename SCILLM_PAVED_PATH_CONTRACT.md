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

JSON validation (strict mode)
- Opt in with `SCILLM_JSON_STRICT=1` (or `strict_json=True` on the call). When enabled and `response_format={"type":"json_object"}` is set, SCILLM raises `JsonParseError` on empty/non‑JSON content and attaches `scillm_meta` with `reason=json_parse_failed`, `sample`, `raw_len`, `model`, and `provider`. This keeps errors actionable without bespoke wrappers.

Parallel batch (openai_like / Chutes)
- Signature (v1.77.4): `parallel_acompletions(requests, *, api_base, api_key, custom_llm_provider='openai_like', concurrency=6, timeout=20, wall_time_s=900, default_max_tokens=None, default_temperature=None, response_format=None, tenacious=True, …)`
- Each request dict may contain: `model`, `messages`, `max_tokens?`, `temperature?`, `response_format?`, `api_base?`, `api_key?`.
- Return shape: list of dicts with `index, request, response, error, status, content`. When `response_format` is json_object, `content` may be a dict or string. Check `error`/`status` per item.
- Guards: if `api_base`/`api_key` or `model` are missing after env defaults, SCILLM raises `ValueError` early instead of hanging.
- Recommended defaults to avoid silent waits: `tenacious=False`, `timeout=20-30`, `wall_time_s=120-300`, `concurrency=4-8`, `response_format={"type":"json_object"}`. Keep `SCILLM_JSON_STRICT=1` in CI to surface bad JSON.
- Structured JSON helpers:
  - `schema=` (jsonschema dict or callable) validates each item’s parsed JSON; failures set `error="invalid_json: …"` and keep `raw` sample.
  - `retry_invalid_json=N` retries invalid JSON up to N times with backoff (same messages).
  - `summary` attached to the first item: counts of ok/invalid_json/provider_error/empty_content.
  - `repair_invalid_json=True` (defaults to env `SCILLM_REPAIR_INVALID_JSON`, default on) salvages malformed JSON (trim braces, then `clean_json_string` when available) before failing; repaired items are marked `repaired=true` in results/summary.
- Minimal example:
```python
resps = await parallel_acompletions(
    [
      {"messages":[{"role":"system","content":"Return JSON only."},
                   {"role":"user","content":"Return {\"ok\":true} as JSON."}],
       "response_format":{"type":"json_object"},
       "max_tokens":64,
       "temperature":0,
       "model": os.environ["CHUTES_MODEL_ID"]},
    ],
    api_base=os.environ["CHUTES_API_BASE"],
    api_key=os.environ["CHUTES_API_KEY"],
    custom_llm_provider="openai_like",
    concurrency=4,
    timeout=20,
    wall_time_s=120,
    response_format={"type":"json_object"},
    tenacious=False,
)
for r in resps:
    if r["error"]:
        print("error", r["status"], r["error"])
    else:
        print("content", r["content"])
```

Debugging quick-guide (Chutes)
- Text sanity (JSON echo):
```
curl -sS -H "Authorization: Bearer $CHUTES_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"'"$CHUTES_MODEL_ID"'","messages":[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],"response_format":{"type":"json_object"},"max_tokens":64,"temperature":0}' \
  "$CHUTES_API_BASE/chat/completions"
```
Expect HTTP 200 and body containing `"ok":true`.
- Multimodal sanity (remote image URL, still returns JSON):
```
curl -sS -H "Authorization: Bearer $CHUTES_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"'"$CHUTES_VLM_MODEL"'","messages":[{"role":"user","content":[{"type":"text","text":"Describe the image in one JSON object with key desc"},{"type":"image_url","image_url":{"url":"https://picsum.photos/seed/scillm/256/256"}}]}],"response_format":{"type":"json_object"},"max_tokens":128,"temperature":0}' \
  "$CHUTES_API_BASE/chat/completions"
```
Expect HTTP 200 and a JSON object (e.g., `{"desc":"..."}`). If these fail (non‑200 or empty), the upstream is down; parallel_acompletions will also fail.

Packaging expectations
- `pip install scillm>=1.77.3` ships the paved helpers (`scillm.paved.*`) **and** the `chutes.middleware.*` modules they depend on. If an ImportError still occurs, upgrade or reinstall the wheel instead of patching a venv manually.
- The `openai_like` provider now accepts Bearer-only auth. Pass `api_key=` and SciLLM will project the token into the correct header (Bearer or `x-api-key`) for Chutes.

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
- 2025‑11‑10: Documented bundled middleware + Bearer-only provider so DevOps doesn’t patch venvs manually.
