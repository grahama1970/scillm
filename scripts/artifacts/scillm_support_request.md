# SciLLM Support Request — OpenAI‑compatible Gateway Interop (Chutes)

Date (UTC): 2025-10-23
Contact: Extractor Agent (on behalf of SciLLM users)

## Summary

We’re seeing avoidable failures when calling an OpenAI‑compatible gateway (Chutes) via SciLLM. The issues come from provider/auth/base normalization gaps and model alias routing. Below are concrete repros, observed behaviors, and the changes we’re requesting from the SciLLM team.

## Environment

- Base: `CHUTES_API_BASE=https://llm.chutes.ai/v1`
- Key: `CHUTES_API_KEY=***` (valid; do not log)
- Client: SciLLM (current HEAD of experiments/litellm)
- Python 3.11/3.12; uv virtualenv

## Problems

1) Unmapped provider when using the OpenAI‑compatible surface
   - Symptom: `custom_llm_provider="openai_like"` + `extra_headers={"x-api-key": ...}` can raise provider errors ("Unmapped LLM provider"), depending on internal routing.

2) Auth header style not normalized
   - Symptom: Gateway rejects `Authorization: Bearer <key>` with 401, but accepts `x-api-key: <key>` (and sometimes raw `Authorization: <key>`).
   - `curl` shows:
     - `Authorization: Bearer …` → 401
     - `x-api-key: …` → 200 (JSON list)

3) Base normalization drift (`/v1` vs no suffix)
   - Symptom: 404 on `/chat/completions` when callers pass a base with/without `/v1` and the internal path logic assumes the other form.

4) Vendor model ids returned by `/v1/models` cause 404/timeouts
   - Symptom: Model ids like `Qwen/Qwen3-235B-A22B-Instruct-2507` 404 on chat routes unless callers hand‑map to a supported alias/provider.

## Expected Behavior

- SciLLM should make OpenAI‑compatible gateways work out‑of‑the‑box:
  - Auto‑detect and normalize base (with/without `/v1`).
  - Auto‑negotiate auth style (`x-api-key`, `Bearer`, or raw `Authorization`).
  - Resolve model aliases from `/v1/models` to route without the caller specifying `custom_llm_provider`.
  - Treat 401/404 responses as signals to adjust headers/base and retry before surfacing errors.

## Minimal Repros (copy/paste)

### A) Header style check (models)

```bash
curl -sS -D - -o /dev/null -H "Authorization: Bearer $CHUTES_API_KEY" "$CHUTES_API_BASE/models"
curl -sS -D - -o /dev/null -H "x-api-key: $CHUTES_API_KEY" "$CHUTES_API_BASE/models"
```

Expected: 200 only when using `x-api-key` (or raw `Authorization`).

### B) SciLLM call (JSON mode)

```python
from scillm import completion
out = completion(
    model="<MODEL_ID>",
    api_base=os.environ["CHUTES_API_BASE"],
    api_key=None,
    custom_llm_provider="openai_like",
    messages=[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
    response_format={"type":"json_object"},
    extra_headers={"x-api-key": os.environ["CHUTES_API_KEY"]},
)
print(out["choices"][0]["message"]["content"])  # expect {"ok":true}
```

Observed intermittently: provider errors or 404 without base/auth normalization.

## Request (concrete changes in SciLLM)

1) Provider/auth/base negotiation
   - On first failure (401/404), detect:
     - Auth style: retry with `x-api-key` if `Bearer` fails; accept raw `Authorization` as a fallback when a gateway supports it.
     - Base normalization: retry the same call after toggling base suffix (`…/v1` ↔ `…`).
   - Cache per‑base auth style + normalized base for subsequent calls.

2) Model alias resolution
   - When `/v1/models` returns vendor‑style ids, transparently route using the OpenAI‑compatible transport (no caller‑side `custom_llm_provider` required).
   - Provide a small mapping seam for known providers (e.g., `Qwen/…` → OpenAI‑compatible path) and fall back to the transport that succeeds.

3) Caller‑facing stability
   - Treat 401/404 as internal retry cues before surface errors.
   - Do not require users to pass `extra_headers` for x‑api‑key if the gateway is OpenAI‑compatible.

## What we already changed (client‑side)

- Enforced SciLLM‑first shims in our pipeline (Stages 06/07/09).
- Replaced ad‑hoc litellm usage with SciLLM wrappers that can pass `x-api-key` when required.
- Added artifacts, minimal repro scripts, and logs under `scripts/artifacts/`.

## Attachments / Artifacts

- `scripts/artifacts/chutes_models_header_check.log` — models header probe (Bearer vs x‑api‑key)
- `scripts/artifacts/chutes_client_text_sanity.json` — minimal JSON‑mode success with x‑api‑key
- `scripts/artifacts/scillm_min_repro.py` — 10‑line repro for SciLLM JSON call
- `scripts/artifacts/scillm_router_sanity.json` — Router‑based repro (if applicable)

## Acceptance Criteria

- SciLLM completion() to a Chutes base returns JSON content with no caller‑side provider/header tweaks.
- A `/v1/models` vendor id works directly in completion() without setting `custom_llm_provider`.
- 401/404 paths perform one internal retry with normalized base/auth and then succeed (or return a single, clear error if the gateway is truly unreachable/invalid).

## Optional (future‑proofing)

- Add DEBUG‑only meta in logs: `scillm_transport=openai-sdk|httpx-oai` and `scillm_auth_style=bearer|x-api-key|raw` (names only; never tokens).
- Add a doctor script that pings `/v1/models` and performs a single JSON‑mode call using the negotiated style/base, returning a PASS line.

---

If you open an issue, we can attach the artifacts above automatically. We’re happy to validate a PR that adds the negotiation + model aliasing, and then remove our interim `extra_headers` usage.

