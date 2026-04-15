from __future__ import annotations
import json, os, typer
from typing import Any, Dict, List

app = typer.Typer(add_completion=False)

@app.command()
def quality(
    file: str = typer.Option("", help="Path to OBS_QUALITY_FILE JSON; defaults to env OBS_QUALITY_FILE"),
    k: int = typer.Option(5, help="Top-K for Recall@K and MRR"),
    json_out: bool = typer.Option(True, "--json"),
):
    """Compute simple Recall@K and MRR over a small local corpus of queries.

    File format (JSON):
      {"queries": [ {"q": "...", "scope": "tabbed", "expected_keys": ["abc123"], "expected_titles": ["...optional..."]}, ... ]}
    Only one of expected_keys/expected_titles is required per query.
    """
    path = file or os.getenv('OBS_QUALITY_FILE') or ''
    if not path:
        print(json.dumps({"meta": {"k": k}, "items": [], "errors": ["OBS_QUALITY_FILE not provided"]}))
        raise SystemExit(2)
    data = json.load(open(path, 'r'))
    from ..api import search as api_search
    items: List[Dict[str, Any]] = []
    ranks: List[int] = []
    for qobj in data.get('queries', []):
        q = qobj.get('q') or ''
        scope = qobj.get('scope') or ''
        expected_keys = [str(x).replace('lessons/','') for x in (qobj.get('expected_keys') or [])]
        expected_titles = [str(x) for x in (qobj.get('expected_titles') or [])]
        res = api_search(q=q, scope=scope, k=k)
        arr = res.get('items') or []
        rank = 0
        for idx, it in enumerate(arr):
            key = str(it.get('_key') or '')
            ttl = str(it.get('title') or '')
            if (expected_keys and key in expected_keys) or (expected_titles and ttl in expected_titles):
                rank = idx + 1
                break
        hit = bool(rank and rank <= k)
        rr = (1.0 / rank) if rank else 0.0
        items.append({"q": q, "scope": scope, "rank": rank, "rr": rr, "hit": hit})
        if rank:
            ranks.append(rank)
    recall_at_k = sum(1 for it in items if it['hit']) / max(1, len(items))
    mrr = sum(it['rr'] for it in items) / max(1.0, float(len(items)))
    out = {"meta": {"k": k, "file": path, "recall_at_k": recall_at_k, "mrr": mrr}, "items": items, "errors": []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
