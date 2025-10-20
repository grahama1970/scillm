Fork: {FORK}
Branch: {BRANCH}
Path: {GIT_SSH_PATH}

Request: Comprehensive review + corrective patches for Option A (autogen + MCTS + judge) to make it fully reliable and self‑diagnosable. Return ONLY a hosted patch URL and a Makefile apply command; do not paste diff contents inline.
Do NOT open a pull request. Answer in this chat only.

Context
- Goal: Make SciLLM Option A “mcts:auto + codex‑judge” work end‑to‑end with clear debug and robust timeouts.
- Current status: Autogen now returns N variants and MCTS runs; judge step misroutes to OpenAI provider if not forced, and some params unsupported on codex‑agent.
- Debug added: per‑request trace id, autogen timing/ids, MCTS knobs/result, provider pre/post HTTP with elapsed, judge parse logs.
- Environment: codex‑agent sidecar on http://127.0.0.1:8089 using ~/.codex/auth.json; no OPENAI_API_KEY.
- Bridge served from litellm/src codeworld.bridge.server; Option B verified; Option A partially green pending judge call hardening.

Review Scope (relative paths)
- Primary:
  - litellm/llms/codeworld.py
  - src/codeworld/bridge/server.py
  - litellm/extras/multi_agents.py
  - debug/demo_scillm_code_mcts_autogen_and_judge_verbose.py
- Scenarios & scripts:
  - debug/demo_scillm_code_mcts_live.py
  - local/docker/compose.agents.yml

Objectives
- Always generate exactly N variants for mcts:auto; surface them in response (code_variants) for judge.
- Judge must use codex‑agent (sidecar) without requiring OPENAI_API_KEY; strip unsupported params.
- Timeouts: align provider request_timeout and bridge autogen timeout; avoid premature ReadTimeouts.
- Debug: preserve current trace logs; add missing timeout values to autogen HTTP call logs.
- Robustness: tolerate nearly‑valid JSON from generator; safe parse with small preview on error.
- No behavior regressions for Option B.

Deliverables (strict)
- FINDINGS: prioritized bullets with impact
- QUESTIONS: concise
- PATCH: Provide a SINGLE hosted git‑compatible patch URL (applies cleanly on {BRANCH}); do not paste diffs inline.

Patch Constraints
- No symbolic hunk headers; use numbered ranges
- Include a one‑line commit subject
- Do not introduce new runtime deps

Current Repro (must remain green after patch)
- Autogen + MCTS + judge verbose demo:
  - SCILLM_ENABLE_CODEWORLD=1 SCILLM_DEBUG=1 \
    CODEX_AGENT_API_BASE=http://127.0.0.1:8089 \
    CODEWORLD_AUTOGEN_HTTP_TIMEOUT_S=120 \
    python debug/demo_scillm_code_mcts_autogen_and_judge_verbose.py
- Expect:
  - variants_present True
  - mcts_best_value present
  - judge {best_id: <id>, rationale_short: <str>}
  - Provider/bridge logs show same trace_id and autogen parsed_variants=N

Known Issues To Fix
1) Judge misrouting and unsupported params
   - Ensure codex_judge_codeworld_result always sets custom_llm_provider="codex-agent" and api_base from CODEX_AGENT_API_BASE; remove unsupported params (e.g., reasoning_effort) or set litellm.drop_params=True locally for that call.
2) Timeout alignment
   - Ensure answer_code_mcts_autogen_and_judge(timeout=…) drives both provider request_timeout and bridge autogen HTTP timeout; add explicit autogen timeout value into server log line.
3) Parsing resilience
   - Harden _mcts_extract_variants_from_raw to accept minor JSON mistakes; log truncated raw preview on parse failure; keep strict ids list when success.
4) Docs/demos
   - Update FEATURES.md and QUICKSTART.md snippets to include Option A exact call and envs; confirm demo scripts reference codex‑agent path.

Acceptance Tests
- Option A end‑to‑end returns 200 with results[0].code_variants size==N, results[0].mcts.best_variant present, and judge.best_id present.
- Sidecar health without OPENAI_API_KEY.
- Logs feature: one trace_id across provider and bridge; show autogen n, timeout_s, parsed_variants, and mcts best.

Output Format (must match exactly)
PATCH_URL: <https://…valid patch link>
APPLY_CMD: make patch.apply-url URL="<PATCH_URL>" BRANCH="feat/copilot-review-option-a"
FINDINGS:
- …
- …
QUESTIONS:
- …
- …

