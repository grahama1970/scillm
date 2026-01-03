# Context — SciLLM Paved Path (Nov 9, 2025)

Single source of truth for how agents and projects use SciLLM in this repo. Keep it practical, minimal, and stable.

## What “Paved Path” Means
- Call SciLLM directly: `acompletion`, `parallel_acompletions`, and (optionally) `Router`.
- No bespoke wrappers, no raw `/chat/completions` in projects, no custom headers. Projects pass `api_base`, `api_key`, `model` — SciLLM canonicalizes auth and handles retries.
- Tenacious retries and bounded concurrency are built in. Do not re‑implement retry loops in projects.

## Required Environment (Chutes)
- `CHUTES_API_BASE` (includes `/v1`)
- `CHUTES_API_KEY`
- Text model: `CHUTES_MODEL_ID` (preferred) or `CHUTES_TEXT_MODEL`
- Vision model: `CHUTES_VLM_MODEL` (for multimodal)

## Sanity (Batch, Text + VLM)
- Script: `scripts/sanity/chutes_batch_sanity.py`
- Make target: `make scillm-sanity`
- What it does: runs 5 requests (JSON probe, France/Paris, HTTPS image, local image file, inline HTML classification) via a single `parallel_acompletions` call. Prints one JSON summary `{ok,count,items[]}` and exits 0/1.
- Inline fixtures: the HTML classification case reads `scripts/sanity/assets/inline_classification.html` (token `luminous-harvest`) to prove html→text expansion, and the local panda photo proves file-path image embedding stays wired up.

## Batch API (Canonical)
- Import: `from scillm import parallel_acompletions`
- Request list: minimal dicts — e.g. `{ "messages": [...], "response_format": {"type":"json_object"} }`. Model defaults resolve from env when omitted.
- Tenacity: on by default for transient 429/5xx/timeout/capacity. Exponential backoff with cap; per‑item wall‑time budget.
- Result shape (per item): `{ index, request, response, error, status, content }`.
- Inline IO convenience is inside the API:
  - `url` or `urls[]` → fetch, HTML→text, append as user message.
  - `file_path` or `paths[]` → read and append; image files auto‑embed as `image_url` (data URL) when `SCILLM_AUTO_IMAGE_DATAURL=1` (default on).

## Router (Optional)
- Build a fixed `model_list` with text and VLM deployments (from env).
- Pass `model_list=` to `parallel_acompletions`; the API auto‑chooses VLM when messages contain `image_url` parts; otherwise text.
- Parallel results retain full `request` and raw `response` for audit.

## Preflight
- Default policy is auto: races strict‑JSON and normal chat probes; accepts the first success. Avoid model‑specific hard‑coding.
- Projects should not implement preflight; rely on the sanity script when needed.

## Do / Don’t
- Do: call SciLLM directly; keep requests minimal; rely on built‑in tenacity; use the sanity script and `make scillm-sanity`.
- Don’t: add wrappers, conditional import hacks, manual Authorization headers, or raw `/chat/completions` calls in projects.

## Troubleshooting
- If `/v1/models` is 200 but `/v1/chat/completions` intermittently returns 401/503: tenacity will back off; allow the batch to run to its wall budget. For strict JSON, prefer a JSON-capable model or let preflight auto-select.
- For multimodal, ensure `CHUTES_VLM_MODEL` is set; local image files require `SCILLM_AUTO_IMAGE_DATAURL=1` (default on).
- Downstream repos (`../extractor`, `../devops`, `../amd`, `../pi-mono`) pin SciLLM via `scillm @ file:///home/graham/workspace/experiments/litellm`. After any change run `./scripts/update_scillm_dependents.sh` so their environments pick up the new editable build before rerunning doctors/pipelines.
- If a router call returns a successful HTTP response but `choices[0].message.content` is empty, downstream stages (e.g., Stage 07 reflow, Stage 09 summarizer) now call `_direct_scillm_json` helpers to replay the same payload via `scillm.acompletion` automatically. Treat these as upstream model issues; the fallback is built in, so no local shims are required.

## Where to Look
- Batch implementation: `scillm/batch.py` (semaphore, backoff, result objects)
- Inline IO expansion: `scillm/preprocess.py` (`url`, `file_path`, `urls`, `paths`)
- Sanity script: `scripts/sanity/chutes_batch_sanity.py`
- Quickstart and feature matrices: `QUICKSTART.md`, `FEATURES.md`

## Stability & Contracts
- The paved path above (APIs, result shapes, envs, sanity location) is a stability contract. Any change requires updating this file, QUICKSTART, and the sanity script together.
