# Feature Recipes

Feature recipes are small, runnable examples that show exactly how a human (or
agent) should invoke a capability. They print a JSON `example_request` and an
`example_response` so callers can see the inputs/outputs without reading code.

Patterns
- Bridge client: talk to a provider’s HTTP bridge (`/bridge/complete`).
  - CodeWorld: `feature_recipes/codeworld_provider.py`, `scenarios/codeworld_bridge_release.py`
  - Lean4: `feature_recipes/lean4_bridge_client.py`, `scenarios/lean4_bridge_release.py`
- Router usage: call through LiteLLM Router using a custom provider.
  - Lean4: `scenarios/lean4_router_release.py` (env: `LITELLM_ENABLE_LEAN4=1`)
  - CodeWorld: `scenarios/codeworld_router_release.py` (env: `LITELLM_ENABLE_CODEWORLD=1`)

Env knobs
- CodeWorld: `CODEWORLD_BASE`, `CODEWORLD_TOKEN`, `CODEWORLD_METRICS`,
  `CODEWORLD_ITERATIONS`, `CODEWORLD_ALLOWED_LANGUAGES`, `CODEWORLD_TIMEOUT_SECONDS`
- Lean4: `LEAN4_BRIDGE_BASE`, `LEAN4_BRIDGE_FLAGS`, `LEAN4_REPO` (for CLI-based recipes)

Provider registration (Router)
- Set `LITELLM_ENABLE_CODEWORLD=1` and/or `LITELLM_ENABLE_LEAN4=1` to enable custom providers in Router.

Side‑by‑side quick reference
- See `feature_recipes/SIDE_BY_SIDE.md` for identical request payloads for both bridges and Router snippets.

Result envelope (target)
- Both bridges should return:
  - `summary` (items, succeeded/failed or proved/failed/unproved)
  - `statistics` (provider-specific counters)
  - `results`/`proof_results` (per-item outputs)
  - `duration_ms`, `stdout`, `stderr`
  - errors are returned with HTTP error codes and structured bodies
