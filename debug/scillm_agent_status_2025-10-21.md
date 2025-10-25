— Begin message to scillm agent —

Summary
- Auth header resolved for Chat: use Authorization: Bearer (not x‑api‑key) on the OpenAI‑compatible path. We’ve locked Extractor calls to one paved shape: `custom_llm_provider='openai_like'`, `api_key=None`, `extra_headers` with Bearer + Content‑Type/Accept, `api_base=$CHUTES_API_BASE`.
- Core scillm paths are stable under this shape. Local smokes pass for direct, Router, parallel_acompletions, and completion(model_list).
- Extractor “doctor” JSON smoke is green. Remaining risk is occasional 429 capacity on live gateway.

What’s green
- scillm → Chutes (OpenAI‑compatible, bearer):
  - Direct JSON: PASS
  - Router.completion: PASS
  - Router.parallel_acompletions: PASS (2/2)
  - completion(model_list): PASS
- Extractor doctor: JSON strict via helper: PASS

Open item
- Capacity 429 appears intermittently on live Chat. We handle one short retry today; recommend honoring Retry‑After if present and adding jittered backoff for the “checkpoint” sub‑call in Stage 09.

Verification snapshot
- Verified: yes
- Timestamp (UTC): 2025‑10‑21T19:35:00Z
- Commands and exits:
  - `python debug/chutes_gateway_preflight.py` → 0
    - MODELS 200; CHAT_auth_bearer 200 PASS; CHAT_x_api_key 401 FAIL; CHAT_auth_raw 429 PASS CAPACITY
  - `CHUTES_AUTH_STYLE=bearer python debug/scillm_openai_compatible_smoke.py` → 0
    - DIRECT_JSON PASS; ROUTER_JSON PASS; PARALLEL_JSON PASS 2/2; MODEL_LIST_JSON PASS
  - `python scripts/doctor/scillm_chutes_doctor.py` → 0
    - MODELS_HTTP 200; JSON_SMOKE_OK 1 1; VLM_SMOKE_SKIPPED (set CHUTES_VLM_MODEL to enable)

Env and call shape
- Base: `CHUTES_API_BASE=https://llm.chutes.ai/v1`
- Auth for Chat: `Authorization: Bearer $CHUTES_API_KEY`
- Headers (Chat): `{ 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json', 'Accept': 'application/json' }`
- Models (current run):
  - `CHUTES_TEXT_MODEL=Qwen/Qwen3‑235B‑A22B‑Instruct‑2507`
  - `CHUTES_VLM_MODEL=Qwen/Qwen3‑VL‑235B‑A22B‑Instruct` (set to enable VLM smoke)

Requests
- Please confirm Retry‑After behavior on 429 for Chat; we’ll honor it and add jittered backoff in the helper.
- Any recommended global QPS/budget guidance for steady‑state usage, so we can parameterize windowing in Extractor doctor and checkpoint calls.
- OK to update QUICKSTART to state: “For Chutes Chat, use bearer (x‑api‑key works for /models but is rejected on Chat).”

Artifacts
- `debug/chutes_gateway_preflight.py` (stdout)
- `debug/scillm_openai_compatible_smoke.py` (stdout)
- `scripts/doctor/scillm_chutes_doctor.py` (stdout)

— End message to scillm agent —

