# Session Context — SciLLM (Oct 19, 2025)

Concise snapshot to resume work after reboot. This reflects today’s verified state, the exact commands to re‑run Option A judge, and the key code changes enabling “model‑only” UX.

## What’s Green Right Now
- Model‑only routing works:
  - completion(model="codex-agent/gpt-5", api_base=http://127.0.0.1:8089, messages=[...], response_format={"type":"json_object"}) returns 200 with strict JSON.
- Option A (codex‑only autogen + judge) is verified end‑to‑end:
  - Script: `python debug/run_option_a_and_write_artifacts.py`
  - Artifacts written:
    - `.artifacts/option_a/winners.jsonl`
    - `.artifacts/option_a/leaderboard.csv`
- Sidecar status: `curl -s http://127.0.0.1:8089/healthz` → `{ "ok": true, "forward_mode": false }`

## Minimal Re‑run (after reboot)
1) Activate venv
   - `source .venv/bin/activate` (if present) or `uv sync`
2) Ensure sidecar up (local codex‑agent; no forwarding)
   - Expect port 8089; verify: `curl -s http://127.0.0.1:8089/healthz`
3) Run smokes
   - Model‑only sanity:
     - `python debug/smoke_model_only_codex_agent.py`
   - Option A judge (writes artifacts):
     - `SCILLM_DEBUG=1 CODEX_AGENT_API_BASE=http://127.0.0.1:8089 \
        python debug/run_option_a_and_write_artifacts.py`

## Code Changes Enabling Model‑Only UX
- File: `litellm/main.py`
  - Added explicit `codex-agent` provider branch before OpenAI handling, preventing accidental fallback to OpenAI when model resolves to `gpt-5`.
  - Imported `CodexAgentLLM` and route calls when `custom_llm_provider == "codex-agent"`.
- Smoke scripts added:
  - `debug/smoke_model_only_codex_agent.py` — strict JSON ping using only `model="codex-agent/gpt-5"`.
  - `debug/run_option_a_and_write_artifacts.py` — generates 6 variants and writes winners/leaderboard.

## Doc Touch‑ups
- `QUICKSTART.md`, `README.md`, `FEATURES.md` updated with the model‑only example and allowed_openai_params note for reasoning fields.

## Env Expectations
- Sidecar base (no /v1):
  - `export CODEX_AGENT_API_BASE=http://127.0.0.1:8089`
- No OPENAI_API_KEY required for codex‑agent path.
- Optional debug:
  - `export SCILLM_DEBUG=1` (client debug)

## Troubleshooting Quicklist
- 404 on /v1/chat/completions → use a model from `/v1/models` (via sidecar).
- Client error about OPENAI_API_KEY → verify you used `model="codex-agent/gpt-5"` or `custom_llm_provider="codex-agent"`.
- Connection refused → confirm sidecar port (8089) and process running.

## Next Optional Tasks
- Make target: add `make option-a-judge` wrapper.
- Add integration test to assert model‑only routing stays green.
- Experimental: try Codex‑Cloud best‑of‑N path
  - `cd scillm/extras/js && npm install`
  - `export SCILLM_EXPERIMENTAL_CODEX_CLOUD=1 CODEX_CLOUD_API_KEY=...`
  - Removed experimental smokes referencing codex‑cloud.
 - OpenAPI draft for Codex Cloud
   - See: `docs/openapi/codex_cloud.yaml` (proposed `/wham/*` endpoints)
 - CI probe (manual)
   - Workflow: `.github/workflows/codex-cloud-smoke.yml` (runs on workflow_dispatch when env+secret are configured)
## 2025-10-20 — Reset: remove Codex‑Cloud lane; keep only live, supported paths

Why this change
- Codex Cloud “tasks” (create task → poll → fetch diff) does not have a public, stable HTTP API. Keeping a seemingly‑live lane created confusion and wasted time.
- We now hard‑disable the codex‑cloud helpers by default and document the supported live paths you can run today.

What changed in this repo
- Disabled/Deprecated codex‑cloud helpers:
  - `scillm/extras/codex_cloud.py`: raises a clear error unless both `SCILLM_ENABLE_CODEX_CLOUD=1` and `SCILLM_EXPERIMENTAL_CODEX_CLOUD=1` are set. Prefer `codex-agent` or your OpenAI‑compatible gateway.
- Makefile cleanup:
  - Removed targets: `codex-cloud-smoke`, `codex-cloud-variants`, `codex-cloud-live`, `codex-cloud-live-variants`.
- Docs:
  - `QUICKSTART.md`: section renamed to “Codex‑Cloud (Deprecated / Disabled by default)” and points users to the live gateway or codex‑agent paths.
- Live utility scripts (gateway‑based, supported now):
  - `debug/live_best_of_n_and_judge.py` — generates N variants via your OpenAI‑compatible `/v1/chat/completions` base and judges them; prints a single JSON with variants and a winner.
  - `debug/live_best_of_n_chat.py` — minimal best‑of‑N generator via the same base.

Clear separation (avoid future confusion)
- Codex Cloud (OpenAI): task/delegation system. No public, stable HTTP Tasks API at this time.
- Gateway (e.g., Chutes at `https://llm.chutes.ai`): OpenAI‑compatible `/v1/chat/completions`. This is what our “best‑of‑N + judge” runs against today.
- Codex‑agent: your local/sidecar provider for chat/generation; also a supported live path.

How to run the live best‑of‑N + judge now (uses gateway)
1) Load `.env` and derive envs (no `/v1` suffix on base):
   - `CODEX_CLOUD_TASKS_BASE_URL=${CHUTES_API_BASE%/}` then trim `/v1` if present
   - `CODEX_CLOUD_TOKEN=$CHUTES_API_KEY`
   - `CODEX_CLOUD_MODEL=${CHUTES_MODEL:-deepseek-ai/DeepSeek-R1}`
   - `JUDGE_MODEL=${JUDGE_MODEL:-deepseek-ai/DeepSeek-R1}` (or another known‑working chat model)
   - `BEST_OF_N=3` (start with 3 to avoid long console output)
2) Run:
   - `PYTHONPATH=$PWD python debug/live_best_of_n_and_judge.py`
3) Expect:
   - Exit 0
   - Printed JSON: `{ base, gen_model, judge_model, variants:[...], judge:{ best_id, reason } }`

Notes
- We verified live calls against `https://llm.chutes.ai` with the above envs; chat returns 200 and non‑empty content. Large BEST_OF_N values can hit console/time caps due to long model outputs; use 3 first.
- If you want the codex‑cloud lane permanently removed (shim/mocks/scripts), say so and we’ll delete them in a follow‑up PR.
