Codex-Agent Runbook — Lessons Learned (2025-10-06)

Purpose: Avoid repeated pitfalls when wiring codex-agent (mini-agent + sidecar) with Router and parallel_acompletions.

Ports / Endpoints
- Mini-agent (OpenAI-compat shim): http://127.0.0.1:8788 (no /v1 in env)
- Codex sidecar (OpenAI-compat): http://127.0.0.1:8077 (no /v1 in env)
- Base rule: CODEX_AGENT_API_BASE must NOT include '/v1'; provider appends

Provider enablement & import order
- Set LITELLM_ENABLE_CODEX_AGENT=1 BEFORE importing litellm/Router in the process
- Fresh process if env changes; venv-cached imports caused confusion

Auth & echo mode
- Echo on (local smoke): set CODEX_SIDECAR_ECHO=1 → no creds needed
- Echo off (real creds): mount ~/.codex/auth.json into container → /root/.codex/auth.json:ro (or set CODEX_AUTH_PATH)
- Verify auth: debug/check_codex_auth.py --container litellm-codex-agent

Router usage (paved road)
- Prefer model_list with {custom_llm_provider:'codex-agent', api_base, api_key}
- For quick probes, per-request kwargs in parallel_acompletions are OK (custom_llm_provider/api_base/api_key)
- Avoid accidental OpenAI client init: fast-path for codex-agent is in provider resolution

Parallel normalization & meta
- As of commit 050f2857d0, parallel_acompletions always yields dicts and attaches scillm_router (even on exceptions)
- Schema-first behavior: non-JSON echo preserved as content; scillm_router.error_type=invalid_json, json_valid=false
- Optional retries meta (codex-agent): set SCILLM_RETRY_META=1 and CODEX_AGENT_ENABLE_METRICS=1 → scillm_router.retries={attempts,total_sleep_ms,last_retry_after_s}

Vision payload shape (critical)
- Content must be a list of parts (do NOT stringify):
  - {"type":"text","text":"…"}, {"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}

Debug scripts (use first)
- Mini-agent readiness: debug/verify_mini_agent.py
- Codex sidecar readiness: debug/verify_codex_agent_docker.py
- Router parallel echo: debug/codex_parallel_probe.py (prints content + scillm_router)
- Retry meta display: debug/retry_meta_probe.py (codex-agent)
- Turn on detailed logs for parallel errors: SCILLM_DEBUG_PARALLEL=1

Common failure modes we hit (and fixes)
- Placeholder /v1 in API base → remove /v1 from CODEX_AGENT_API_BASE
- Missing auth.json with echo off → 401 from sidecar; mount ~/.codex/auth.json or re-enable echo
- Stale venv (no scillm_router) → install fork commit 050f2857d0; restart process
- OpenAI API key error on codex-agent path → ensure LITELLM_ENABLE_CODEX_AGENT=1 before import; use codex-agent fast-path
- Null content with no meta in parallel → fixed by normalization hook; verify with codex_parallel_probe.py

Docs updated
- README.md, QUICKSTART.md, FEATURES.md contain exact envs, docker mount, probes, and base rule (omit /v1)

Minimal known-good env block
export LITELLM_ENABLE_CODEX_AGENT=1
export CODEX_AGENT_API_BASE=http://127.0.0.1:8077
# export CODEX_AGENT_API_KEY=… (only if gateway enforces)
export SCILLM_RETRY_META=1        # optional (codex-agent)
export CODEX_AGENT_ENABLE_METRICS=1  # optional (codex-agent)

Verification checklist (30s)
- curl $CODEX_AGENT_API_BASE/healthz → ok
- python debug/verify_codex_agent_docker.py → chat OK
- python debug/codex_parallel_probe.py → content + scillm_router present
- For real creds: python debug/check_codex_auth.py → ok