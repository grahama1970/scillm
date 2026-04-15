"""Dynamic Natural Language to AQL Graph Traversal."""
from __future__ import annotations

import json
import re
import requests
import typer
from loguru import logger

from ._helpers import app, _service_post, _json_output

PROMPT = """You are an ArangoDB AQL expert. Convert the user's natural language query into a single, valid, read-only AQL query.

### Graph Schema
We have the following collections:
- `binary_features` (Document Collection): represents nodes. Fields: `_key`, `_id`, `label`, `nodeType` (rpc, event, schema, state_machine), `cluster`, `tier`
- `binary_feature_edges` (Edge Collection): represents connections. Fields: `_key`, `_id`, `_from`, `_to`, `edge_type`
- `lessons` (Document Collection): textual knowledge nodes. Fields: `_key`, `_id`, `title`, `scope`
- `lesson_edges` (Edge Collection): connects lessons. Fields: `_from`, `_to`, `type`
- `sparta_qra` (Document Collection): SPARTA cybersecurity QRA lessons.

### Instructions
1. Output NO explanation.
2. Return ONLY the raw AQL wrapped in a markdown codeblock ```aql ... ```.
3. The AQL MUST be read-only (NO INSERT, UPDATE, REMOVE, UPSERT).
4. If a query implies paths or hops, use AQL graph traversal syntax. For example, to find 2-hop neighbors of 'list_mcp_tools', you might use:
   `FOR v, e, p IN 1..2 ANY 'binary_features/list_mcp_tools' binary_feature_edges RETURN v`
5. If searching by string, use `FILTER v.label == '...'` or `FILTER LIKE(v.label, '%...%')`.

### User Query
{query}
"""

@app.command("explore")
def explore_cmd(
    q: str = typer.Option(..., "--q", "-q", help="Natural language query to translate to AQL"),
    scope: str = typer.Option("", "--scope", "-s", help="Project scope"),
) -> None:
    """Explore the graph dynamically by converting natural language to AQL and executing it."""
    try:
        # 1. Call LLM to generate AQL using the central proxy
        res = requests.post("http://127.0.0.1:4001/v1/chat/completions", json={
            "model": "text",
            "messages": [{"role": "user", "content": PROMPT.format(query=q)}],
            "temperature": 0.0,
        }, timeout=30)
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"]
        
        # 2. Extract AQL from markdown block
        match = re.search(r"```(?:aql|AQL)?(.*?)```", content, re.DOTALL)
        aql = match.group(1).strip() if match else content.strip()
        
        # 3. Execute via the memory daemon (safe read-only endpoint)
        result = _service_post("/query", {"aql": aql})
        
        # 4. Output the results alongside the generated query for transparency
        _json_output({
            "query": q,
            "generated_aql": aql,
            "result": result
        })
    except Exception as exc:
        logger.error(f"Explore failed: {exc}")
        _json_output({"error": str(exc)})
