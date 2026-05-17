# scillm exec graph debugger example

This example is the intended UX shape for `scillm.exec.graph.v1` runs.

It treats each DAG node as a debugger frame:

- left pane: live D3/React DAG with node state colors
- right pane: node inspector for prompt, state, payload, response, artifacts, and notes
- event stream: `GET /v1/scillm/exec/{run_id}/events/stream`
- control endpoints: pause/resume/stop/comment/retry nodes through the scillm exec API

## Runtime boundary

`scillm` owns runtime truth: graph status, node status, events, artifacts, and control endpoints.

`/ask` and `/plan-iterate` compile semantic intent into a runtime graph and decide what a completed graph means. `scillm` only reports whether the runtime graph completed, failed, or was stopped.

## React/D3 rules

The component should follow the project rules from `best-practices-react` and `best-practices-d3`:

- React owns DOM; D3 owns math/layout/path generation.
- Use keyed node/edge rendering.
- Use `viewBox` plus `ResizeObserver` for responsive SVG.
- Do not rely on color alone; show status text/icons.
- Every interactive control has `data-qid`, `data-qs-action`, `title`, and a registered action hook in the real ux-lab integration.

## Typical data flow

```text
dag.json
  -> POST /v1/scillm/exec/graph
  -> status.json + events.jsonl + node artifacts
  -> D3 graph updates from SSE
  -> human/project-agent inspects or pauses at breakpoints
```
