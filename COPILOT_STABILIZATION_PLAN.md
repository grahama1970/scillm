# Generalized Copilot Request — Patch + Answers (No PRs, No Links)

**Project**

* Fork/Repo: `experiments/litellm`
* Branch: `stabilize/scillm-core-transport-auth`
* Path: `git@github.com:experiments/litellm.git#stabilize/scillm-core-transport-auth`

**Task**

* Consolidate scillm transport/auth behavior into one paved path; revert hidden magic; add provider smokes; keep public APIs unchanged.

**Context (brief, optional)**

* Recent auto-auth logic created unpredictable behavior (Bearer vs x-api-key) across OpenAI-compatible gateways (e.g., Chutes). Router/parallel/model_list need guaranteed header policy and stable returns without new knobs.
* Do not break certainly/CodeWorld; defaults must remain boring and explicit. New behavior opt-in only; ship a global safe-mode kill switch.

**Review Scope (relative paths)**

* Primary:

  * `litellm/main.py`
  * `litellm/llms/openai_like/chat/handler.py`
  * `litellm/extras/preflight.py`
  * `litellm/router.py`
  * `litellm/router_utils/parallel_acompletion.py`
* Also check (if needed):

  * `tests/local_testing/*` (new smokes)
  * `QUICKSTART.md`, `ENV_REFERENCE.md` (explicit policy and flags)

**Objectives**

* Enforce one transport policy: `api.openai.com` → OpenAI SDK; all other bases → HTTPX OpenAI-compatible; no hidden switching.
* Make experimental auto-auth negotiation default-OFF behind `SCILLM_ENABLE_AUTO_AUTH=1`; add global kill switch `SCILLM_SAFE_MODE=1`.
* Add provider smokes (mock gateway rejects Bearer, accepts x-api-key/raw) that must pass for direct, Router, parallel, and model_list.

**Constraints**

* **Unified diff only**, inline inside a single fenced block.
* **No PRs, no hosted links, no URLs, no extra commentary.**
* Include a **one-line commit subject** inside the patch.
* **Numeric hunk headers only** (`@@ -old,+new @@`), no symbolic headers.
* Patch must apply cleanly on branch `stabilize/scillm-core-transport-auth`.
* Preserve public API contracts; no changes to certainly/CodeWorld surfaces.

**Acceptance (we will validate)**

* `SCILLM_SAFE_MODE=1` → existing behavior unchanged, all current smokes pass.
* Mock provider smokes green: direct, Router, parallel_acompletions, completion(model_list) produce non-empty choices[0].message.content with x-api-key.
* DEBUG logs show chosen transport and auth header style names (not values).

**Deliverables (STRICT — inline only; exactly these sections, in this order)**

1. **UNIFIED_DIFF:**

```diff
<entire unified diff here>
```

2. **ANSWERS:**

* `<Answer to Q1>`
* `<Answer to Q2>`
* `<Answer to Q3>`
* `…`

**Clarifying Questions (answer succinctly in the ANSWERS section; if unknown, reply `TBD` + minimal dependency needed)**

* Dependencies/data sources: Do we need to pin inputs/models/versions for repeatability?
* Schema drift: Should exporters/parsers tolerate missing/renamed columns with failing smokes?
* Safety: Are all mutating paths gated behind `--execute`? Any missing guards?
* Tests/smokes: Which deterministic smokes must pass (counts > 0, strict formats)?
* Performance: Any batch sizes, rate limits, or timeouts/retries to honor?
* Observability: What summary lines should the CLI print on completion?

