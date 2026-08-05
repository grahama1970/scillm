# SciLLM Transport Profiles & Normalized Transports

Implements grahama1970/scillm#27 (`scillm.transport_profile.v1` + readiness
discovery) and grahama1970/scillm#28 (normalized model-turn / provider-session
transport API). Tau is the agent harness; SciLLM only carries provider turns.

## Harness boundary

Profiles and transports describe how SciLLM can carry a Tau-controlled model
turn or provider session. They never claim Tau-owned responsibilities: the
agent tool loop, tool authorization/execution, worktree policy, semantic
retry/repair, evidence acceptance, or node/DAG completion. A profile that
advertises such a capability fails validation closed; a transport result
carries `"tau_completion": null` unconditionally.

## Profiles (#27)

```text
GET /v1/scillm/profiles                      # full registry + role aliases
GET /v1/scillm/profiles/capabilities?require=tool_calling,streaming
GET /v1/scillm/profiles/readiness?profile=claude-model-turn&live=true
```

Readiness states: `configured` → `credential_ready` → `transport_live_ready`,
plus `degraded` (explicit reason) and `unavailable`. `transport_live_ready` is
only reported after a real one-shot completion readback (`?live=true`) whose
evidence records the resolved model, upstream request id, and usage —
configuration presence alone never counts as live.

Fail-closed rules:
- unknown profile → 404 `unknown_transport_profile`
- unknown capability → 422 `unknown_transport_capability`
- fallback resolution walks only the profile's declared ordered `fallbacks`
  and never silently downgrades: a chain with no candidate satisfying the
  required capabilities errors (`transport_capability_unsatisfied`) with the
  skipped candidates and their missing capabilities recorded.

### Role aliases (copyable)

Aliases resolve to transport profiles server-side, so `/ask` and Tau never
hard-code models:

```bash
KEY=${SCILLM_MASTER_KEY:?}
for role in coordinator backend frontend documentation testing independent-review; do
  curl -s -H "Authorization: Bearer $KEY" \
    "http://localhost:4001/v1/scillm/profiles/readiness?profile=$role" | jq -c '.readiness[0] | {profile, state}'
done
```

Registry defaults derive from proxy config + OAuth mounts; override or extend
via `local/transport_profiles.yaml` (`profiles:` list, `aliases:` map;
validated fail-closed at load).

## Normalized transports (#28)

Contracts: `scillm.transport_request.v1`, `transport_handle.v1`,
`transport_event.v1`, `transport_result.v1`.

```text
POST /v1/scillm/transports                       # create; runs turn 0
GET  /v1/scillm/transports/{id}                  # handle/state
GET  /v1/scillm/transports/{id}/events?since=N   # typed events
POST /v1/scillm/transports/{id}/turns            # Tau posts tool results / next messages
POST /v1/scillm/transports/{id}/cancel           # cancel in-flight provider turn
GET  /v1/scillm/transports/{id}/result?wait_sec=10
```

### Copyable Tau request

```bash
curl -s -X POST http://localhost:4001/v1/scillm/transports \
  -H "Authorization: Bearer $KEY" -H "X-Caller-Skill: tau" -H 'Content-Type: application/json' -d '{
  "schema": "scillm.transport_request.v1",
  "profile": "claude-model-turn",
  "correlation": {"tau_run_id": "run-1", "node_id": "n1", "attempt": 1, "goal_hash": "abc"},
  "messages": [{"role": "user", "content": "Use the read_file tool, then summarize."}],
  "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
  "required_capabilities": ["tool_calling", "cancellation"]
}'
# → 201 handle {transport_id, ...}. Poll result:
curl -s -H "Authorization: Bearer $KEY" \
  "http://localhost:4001/v1/scillm/transports/$TID/result?wait_sec=10"
# state=awaiting_tool_result → Tau executes the tool itself, then:
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  "http://localhost:4001/v1/scillm/transports/$TID/turns" \
  -d '{"tool_results": [{"tool_call_id": "call_1", "content": "…tool output…"}]}'
# cancel an in-flight turn:
curl -s -X POST -H "Authorization: Bearer $KEY" \
  "http://localhost:4001/v1/scillm/transports/$TID/cancel"
```

### Truthfulness guarantees

- SciLLM never executes tools and never continues an autonomous loop: a
  tool-call turn terminates in `awaiting_tool_result` until Tau posts results.
- Reasoning-only output or empty terminal text is `failed`
  (`reasoning_only_output` / `empty_terminal_text`), never a success.
- Terminal state describes the provider turn only — the result carries an
  explicit note and `tau_completion: null`.
- Steering an in-flight turn returns typed `queued_for_next_turn_unsupported`;
  turns against cancelled/failed transports return typed `unsupported`.
- `opaque_agent_compat` profiles (e.g. `opencode-serve-compat`) cannot be
  driven as Tau-native turns: creation returns 409 `fork_required` pointing at
  the native surface (`/v1/scillm/opencode`), which keeps native run/session/
  event references and honestly reduced capabilities.
- Provider-level transport retries stay inside the chat surface SciLLM owns;
  the transport API surfaces provider errors as typed events for Tau's
  semantic retry to judge — it never re-runs a failed turn itself.

## Proof

- Deterministic: `pytest tests/test_transport_profiles.py tests/test_transports_api.py`
- Live: `python3 scripts/prove_transports_live.py` against a live proxy —
  OAuth one-turn readback, multi-turn tool canary (harness supplies the tool
  result), positive cancellation, and the negative paths above.
