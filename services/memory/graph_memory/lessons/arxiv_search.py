from __future__ import annotations
import time
import json
import typer
from .arxiv_ingest import _fetch_arxiv

app = typer.Typer(add_completion=False)


@app.command()
def search(q: str = typer.Option(..., help="arXiv search query"), max_results: int = typer.Option(5), json_out: bool = typer.Option(True, "--json", help="Output JSON envelope")):
    t0 = time.time()
    items = []
    errors = []
    try:
        items = _fetch_arxiv(q=q, max_results=max_results)
    except Exception as e:
        errors.append(f"fetch failed: {e}")
    took = int((time.time() - t0) * 1000)
    out = {"meta": {"q": q, "count": len(items), "took_ms": took}, "items": items, "errors": errors}
    print(json.dumps(out, ensure_ascii=False))

