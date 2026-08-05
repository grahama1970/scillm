# scillm exec runtime contract

`scillm exec` is the bounded worker/runtime layer for agentic tasks. It centralizes local commands, model calls, batched model calls, Codex exec workers, OpenCode workers, Pi workers, Claude print workers, event logs, status files, and small runtime DAGs.

It is not a project planner.
## Tier boundary (harness-owned)

`scillm exec` is **Tier 1** — deterministic pipelines with LLM at bounded gates
(monitors, checkpoint loops, recover+report). It is **not** the surface for
product code authorship.

| Call kind | Use exec for | Use agents (`/v1/scillm/agents/*`) for |
|-----------|--------------|----------------------------------------|
| Monitor/checkpoint loop | Yes | No |
| One-shot critique/classification | Use chat, not exec | No |
| Multi-file code fix in worktree | No | Yes |

Worker node types such as `codex_exec`, `opencode_exec`, and `pi_exec` remain
available for **bounded subprocess tasks inside exec graphs** (diagnosis,
structured extraction, allowlisted patch probes). The project agent must not
route open-ended product implementation through exec graphs when Tier 2 agents
is the correct tier. See
[docs/interactive-agents/routing.md](interactive-agents/routing.md).

 Callers such as `plan-iterate`, `ask`, `review-code`, or a project agent own semantic goals, phase contracts, review verdicts, and replanning policy. `scillm exec` only reports whether a runtime run completed, failed, or was stopped.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/scillm/exec` | Run one bounded worker node. |
| `POST /v1/scillm/exec/batch` | Run independent worker nodes with bounded concurrency. |
| `POST /v1/scillm/exec/graph` | Run a small dependency DAG of model, worker, and local nodes. |
| `GET /v1/scillm/exec/{run_id}/status` | Read `status.json` for a run. |
| `GET /v1/scillm/exec/{run_id}/events` | Read tail events from `events.jsonl`. |
| `POST /v1/scillm/exec/{run_id}/cancel` | Request cancellation of an active run. |

## Node types

| Node type | Use |
|---|---|
| `local_command` | Deterministic local command: extraction, validation, tests, reducers. |
| `scillm_call` | One LLM/VLM call through `/v1/chat/completions`. |
| `scillm_batch` | One server-side batch through `/v1/scillm/batch/completions`. |
| `codex_exec` | Headless Codex worker subprocess for bounded tasks inside exec graphs (diagnosis, allowlisted probes — not open-ended product code authorship). |
| `opencode_exec` | Headless OpenCode worker subprocess for bounded tasks inside exec graphs through profile-only OpenCode providers such as `oc-chutes-deepseek`. |
| `pi_exec` | Headless Pi CLI worker subprocess for bounded tasks inside exec graphs through profile-only Pi providers such as `pi-chutes-kimi`. |
| `kimi_exec` | Headless Moonshot Kimi CLI one-shot worker (`kimi --print --final-message-only`) for bounded tasks inside exec graphs through profile-only names such as `kimi-k2.6`. |

| `cursor_exec` | Headless Cursor CLI worker via `agent -p --output-format stream-json` for bounded tasks through profile-only Cursor exec lanes such as `cursor-auto`, `cursor-plan`, and `cursor-composer-2.5`. |

`cursor_exec` follows the same profile-only boundary as Pi/OpenCode exec. Use `model: "cursor-auto"` or the CLI shortcut `scillm exec cursor-auto`; do not pass `cursor-auto` through `/v1/chat/completions`. The runtime materializes harness-selected skills under `.scillm/cursor-headless/`, writes a temporary `.cursor/rules/.../RULE.md`, runs `agent -p --trust --workspace "$cwd" --model auto --output-format stream-json --stream-partial-output`, parses the terminal `result` event into a receipt, and fails closed on `metadata.allow_write_paths` violations while ignoring harness paths under `.scillm/**` and the generated rule directory. Auth comes from `CURSOR_API_KEY` in the environment or `~/.zshrc`. Read-only diagnose uses `cursor-plan` (`--mode plan`, no `--force`); bounded writes use `cursor-auto` with `--cursor-force` and explicit `--allow-write` paths.

| `claude_print` | Headless Claude print worker subprocess for bounded repo/code/review tasks. |
| `deterministic_render` | Deterministic report or review-bundle rendering. |
| `deterministic_verifier` | Deterministic validation gate. |

`opencode_exec` does not accept raw Scillm chat profiles such as `chutes-deepseek`, direct `chutes/...` model ids, or Scillm HTTP `opencode-go/*` model ids. Use `model: "oc-chutes-deepseek"` so the runtime owns the OpenCode CLI config, enables only the Chutes provider from `.env`, denies skill/web/shell/subagent tools, and can audit workspace writes. The default backing OpenCode model is `chutes/moonshotai/Kimi-K2.6-TEE`, overridable with `SCILLM_OPENCODE_CHUTES_DEEPSEEK_MODEL`. For Scillm HTTP model calls and batch lanes, keep using existing `opencode-go/*` or `oc-*` routes through `scillm_call` or `scillm_batch`. For `sandbox: "workspace-write"`, set `metadata.allow_write_paths` to the relative files, directories, or globs the worker may change; any other write fails the node with `write_allowlist_violation`.


`codex_exec` uses the same profile-only boundary. Use `model: "codex-gpt-5.5"` or the CLI shortcut `scillm exec codex-gpt-5.5`; do not confuse that with `POST /v1/chat/completions` using `model: "gpt-5.5"` (HTTP chat is a different surface). The runtime invokes `codex exec --json --sandbox <mode> --model <resolved>` with optional `-c reasoning.effort=...` from `reasoning_effort` or `metadata.reasoning_effort`. Override the backing model with `SCILLM_CODEX_EXEC_MODEL` (default `gpt-5.5`) or `SCILLM_CODEX_EXEC_MODEL_VISION` for `codex-vision` (default `gpt-5.3-codex`). CLI overrides: `--codex-model`, `--reasoning-effort`.

```bash
scillm exec codex-gpt-5.5   --cwd /home/graham/workspace/project   --sandbox read-only   --reasoning-effort high   --prompt 'Inspect the bounded failure and return JSON only.'
```

`pi_exec` follows the same profile-only boundary. Use `model: "pi-chutes-kimi"` or the CLI shortcut `scillm exec pi-chutes-kimi`; do not pass raw chat profiles such as `chutes-kimi`, direct `chutes/...` ids, or Scillm HTTP `oc-*` aliases. The runtime invokes the local Pi fork through `/home/graham/bin/pi` by default, configurable with `SCILLM_PI_BINARY`, and uses `pi --mode json --provider chutes --model moonshotai/Kimi-K2.6-TEE --thinking off`. The backing model is overridable with `SCILLM_PI_CHUTES_KIMI_MODEL`. Read-only runs enable Pi read/search tools only; workspace-write runs enable Pi edit/write tools and still fail closed on `metadata.allow_write_paths` violations. Docker mounts `/home/graham/.pi/agent` so the proxy container sees the same Pi Chutes provider registry and auth as the host CLI.

## Single exec example

```json
{
  "run_id": "inspect-parser-failure",
  "id": "inspect_parser_failure",
  "type": "codex_exec",
  "model": "codex-gpt-5.5",
  "sandbox": "read-only",
  "node_goal": "Inspect the parser failure and return a bounded diagnosis.",
  "prompt": "Read the supplied failure bundle. Return JSON with findings, files inspected, and recommended next step.",
  "output_schema": {
    "type": "object",
    "required": ["status", "findings", "files_inspected", "recommended_next_step"]
  }
}
```


`kimi_exec` uses the same profile-only boundary. Use `model: "kimi-k2.6"` or the CLI shortcut `scillm exec kimi-k2.6`; do not pass raw chat profiles such as `oc-kimi` or `opencode-go/kimi-k2.6`. The runtime invokes `kimi --print --final-message-only --output-format stream-json --work-dir <cwd> --model <resolved> -p <prompt>`. Override the backing model with `SCILLM_KIMI_EXEC_MODEL` (default `kimi-k2.6`) or `SCILLM_KIMI_EXEC_MODEL_K25` for `kimi-k2.5`. Authenticate with `KIMI_API_KEY` in the environment. Read-only runs omit `--yolo`; workspace-write runs pass `--yolo` and still fail closed on `metadata.allow_write_paths` violations. Override the binary with `SCILLM_KIMI_BINARY` (default `kimi`). Unlike `codex_exec` or constrained `claude_print`, default `kimi_exec` uses native `kimi -p` with Kimi's auto permission policy and full tool surface (not a hobbled non-interactive lane). For deterministic JSON gate output on older `kimi-cli` builds, set `metadata.kimi_output_mode: "print"` or `SCILLM_KIMI_EXEC_OUTPUT_MODE=print` (`--print --final-message-only --output-format stream-json`).

```bash
scillm exec kimi-k2.6   --sandbox read-only   --prompt "What is 2+2?"
```


Submit:

```bash
curl -s -X POST http://localhost:4001/v1/scillm/exec \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "Content-Type: application/json" \
  -H "X-Caller-Skill: plan-iterate" \
  -d @exec.json | jq .
```

OpenCode-over-Chutes exec has a CLI shortcut:

```bash
scillm exec oc-chutes-deepseek \
  --cwd /home/graham/workspace/project \
  --sandbox read-only \
  --prompt 'Inspect the bounded failure and return JSON only.'
```

Pi-over-Chutes exec has the same shape:

```bash
scillm exec pi-chutes-kimi \
  --cwd /home/graham/workspace/project \
  --sandbox read-only \
  --prompt 'Inspect the bounded failure and return JSON only.'
```

## Exec graph example

```json
{
  "exec_graph_version": "scillm.exec.graph.v1",
  "graph_id": "module-best-of-n",
  "graph_goal": "Generate candidate implementations, test them, judge them, and emit a review bundle.",
  "cwd": "/home/graham/workspace/project",
  "max_concurrency": 4,
  "nodes": [
    {
      "id": "writer_a",
      "type": "codex_exec",
      "model": "codex-gpt-5.5",
      "sandbox": "workspace-write",
      "worktree": {"enabled": true, "base_ref": "HEAD"},
      "node_goal": "Write candidate A using a minimal-diff approach.",
      "prompt": "Implement the bounded module change. Preserve public schema. Return JSON only.",
      "depends_on": []
    },
    {
      "id": "writer_b",
      "type": "opencode_exec",
      "model": "oc-chutes-deepseek",
      "sandbox": "workspace-write",
      "worktree": {"enabled": true, "base_ref": "HEAD"},
      "metadata": {"allow_write_paths": ["src/bounded_module.py", "tests/test_bounded_module.py"]},
      "node_goal": "Write candidate B using a robustness-first approach.",
      "prompt": "Implement the bounded module change. Preserve public schema. Return JSON only.",
      "depends_on": []
    },
    {
      "id": "run_candidate_tests",
      "type": "local_command",
      "node_goal": "Run identical candidate sanity checks.",
      "command": "python scripts/run_candidate_tests.py",
      "depends_on": ["writer_a", "writer_b"]
    },
    {
      "id": "judge",
      "type": "scillm_call",
      "model": "text",
      "node_goal": "Pick the strongest candidate or reject all using only test results.",
      "prompt": "Judge the candidate patches and test logs. Return JSON only.",
      "depends_on": ["run_candidate_tests"]
    }
  ]
}
```

## Runtime artifacts

Each run writes under `$SCILLM_EXEC_OUTPUT_DIR` or `/tmp/scillm-exec` by default:

```text
<run_id>/
  graph.request.json
  events.jsonl
  status.json
  execution_result.json
  nodes/<node_id>/
    node.request.json
    result.json
    attempt-1/
      assembled_prompt.txt
      stdout.log
      stderr.log
      response.json
```

## Validation and local sanity checks

After pulling the change on the workstation, run:

```bash
docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build
pytest tests/test_exec_e2e.py -q
bash scripts/sanity_all_endpoints.sh      # full gate: ops/read surfaces + exec + live runners + standing agent
bash scripts/sanity_ops_endpoints.sh      # ops/read only (no live LLM runners)
bash scripts/sanity_exec_endpoints.sh     # exec HTTP + live runners (fail-closed)
bash scripts/sanity_live_runners.sh       # live runners only (same gate, no local_command section)
```

Endpoint verification is deterministic (`curl` + `jq`). Do **not** use `/ask webgpt` as proof that routes work.

Live runner checks hit real provider CLIs through the proxy. They **fail** when auth is expired or a documented profile is broken — there is no optional/skip path.

## Cursor exec stream supervision

`cursor_exec` runs the Cursor CLI with `--output-format stream-json`. scillm supervises the NDJSON stream incrementally in `exec_api._run_cursor_agent_process`:

- Parse each stdout line as it arrives and append to `.scillm/cursor-headless/<run_ctx>/cursor-events.jsonl` (incremental during the run, not end-only)
- Mirror raw stdout chunks to `<run_id>/nodes/<node_id>/attempt-1/events.jsonl` and run-level `/tmp/scillm-exec/<run_id>/events.jsonl`
- Reset **semantic** idle on `system`, `thinking`, `assistant`, `tool_call`, and `result` events — not raw byte silence during long tool stretches
- Complete successfully when a terminal `{"type":"result","subtype":"success","is_error":false}` arrives — terminate the hung CLI instead of waiting for process exit
- `timeout_s` and `idle_timeout_s` are fail-closed backstops only; do not use them as the primary progress or scheduling signal

**Does not apply:** `/v1/chat/completions` SSE heartbeats, `stream_heartbeat_s`, or Chutes timeout-estimator middleware. Those are chat/batch surfaces only.

### What to monitor (not estimated chat timeout)

| Artifact | Purpose |
|----------|---------|
| `/tmp/scillm-exec/<run_id>/events.jsonl` | Live supervisor trail (stdout/stderr chunks, `emit()` events) |
| `GET /v1/scillm/exec/{run_id}/events?tail=N` | Same file over HTTP while a run is active |
| `.scillm/cursor-headless/<run_ctx>/cursor-events.jsonl` | Parsed stream-json events |
| `result.json` / HTTP response `result` | Terminal receipt: `stream_completed`, `recovered_from_stream`, `result_event`, `tool_call_count`, `text` |

**Success signal:** a stream line with `"type": "result"` and acceptable `subtype` / `is_error`.

**Liveness signal:** continuing semantic stream activity (tool_call started/completed, assistant partials, thinking deltas). Do not infer stuck from elapsed wall time alone while the stream still shows activity.

### Two-call contract (orchestrators)

Typical diagnose → fix pattern (e.g. PDF Lab page repair via `POST /v1/scillm/exec`):

| Call | Profile | Sandbox | Backstop example | Completion |
|------|---------|---------|------------------|------------|
| Call 1 diagnose | `cursor-plan` | `read-only` | `timeout_s` 600, `idle_timeout_s` 120 | Terminal `result` with diagnose JSON |
| Call 2 fix | `cursor-auto` | `workspace-write` + `metadata.allow_write_paths` | `timeout_s` 1200, `idle_timeout_s` 300 | Terminal `result` + allowlist write audit |

Orchestrators may issue a blocking `POST /v1/scillm/exec` and trust server-side supervision, or tail `events.jsonl` / poll `GET .../events` until a terminal `result` appears. Do not reimplement stream-json parsing in callers unless bypassing scillm. Optional: `stream: true` on the exec request for SSE progress (same semantics).

### Other exec runners

`codex_exec`, `pi_exec`, `kimi_exec`, `opencode_exec`, and `claude_print` use **byte-level** stdout/stderr idle watchdogs. Only `cursor_exec` uses semantic NDJSON idle reset.

## Boundaries

`scillm exec graph` may say: the runtime graph completed, failed, or was stopped.

It must not say: the project goal is complete, the phase passed, the schema may change, or a patch is safe to merge. Those decisions belong to the caller and review gates.
