# scillm exec runtime contract

`scillm exec` is the bounded worker/runtime layer for agentic tasks. It centralizes local commands, model calls, batched model calls, Codex exec workers, Claude print workers, event logs, status files, and small runtime DAGs.

It is not a project planner. Callers such as `plan-iterate`, `ask`, `review-code`, or a project agent own semantic goals, phase contracts, review verdicts, and replanning policy. `scillm exec` only reports whether a runtime run completed, failed, or was stopped.

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
| `codex_exec` | Headless Codex worker subprocess for bounded repo/code tasks. |
| `claude_print` | Headless Claude print worker subprocess for bounded repo/code/review tasks. |
| `deterministic_render` | Deterministic report or review-bundle rendering. |
| `deterministic_verifier` | Deterministic validation gate. |

## Single exec example

```json
{
  "run_id": "inspect-parser-failure",
  "id": "inspect_parser_failure",
  "type": "codex_exec",
  "model": "gpt-5.5",
  "sandbox": "read-only",
  "node_goal": "Inspect the parser failure and return a bounded diagnosis.",
  "prompt": "Read the supplied failure bundle. Return JSON with findings, files inspected, and recommended next step.",
  "output_schema": {
    "type": "object",
    "required": ["status", "findings", "files_inspected", "recommended_next_step"]
  }
}
```

Submit:

```bash
curl -s -X POST http://localhost:4001/v1/scillm/exec \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "Content-Type: application/json" \
  -H "X-Caller-Skill: plan-iterate" \
  -d @exec.json | jq .
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
      "model": "gpt-5.5",
      "sandbox": "workspace-write",
      "worktree": {"enabled": true, "base_ref": "HEAD"},
      "node_goal": "Write candidate A using a minimal-diff approach.",
      "prompt": "Implement the bounded module change. Preserve public schema. Return JSON only.",
      "depends_on": []
    },
    {
      "id": "writer_b",
      "type": "claude_print",
      "sandbox": "workspace-write",
      "worktree": {"enabled": true, "base_ref": "HEAD"},
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
bash scripts/sanity_exec_endpoints.sh
```

Optional Codex and Claude smoke checks run only when the corresponding CLIs and auth are available.

## Boundaries

`scillm exec graph` may say: the runtime graph completed, failed, or was stopped.

It must not say: the project goal is complete, the phase passed, the schema may change, or a patch is safe to merge. Those decisions belong to the caller and review gates.
