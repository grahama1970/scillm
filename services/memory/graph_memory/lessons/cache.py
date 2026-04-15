from __future__ import annotations
import typer
from ..arango_client import get_db
from loguru import logger

app = typer.Typer(add_completion=False)


@app.command("stats")
def stats(json_out: bool = typer.Option(False, "--json")):
    db = get_db()
    # Counts for lesson_embeddings, grouped by model
    aql = """
    LET total = LENGTH(lesson_embeddings)
    LET per_model = (
      FOR d IN lesson_embeddings
        COLLECT m = d.model_id WITH COUNT INTO ct
        RETURN { model_id: m, count: ct }
    )
    RETURN { total: total, per_model: per_model }
    """
    out = list(db.aql.execute(aql))[0]
    # Best-effort extra caches
    try:
        out['anchor_cache_count'] = len(list(db.collection('anchor_score_cache')))
    except Exception as exc:
        logger.error("stats collection access failed: {exc}", exc=exc)
        out['anchor_cache_count'] = 0
    try:
        out['qa_cache_count'] = len(list(db.collection('qa_cache')))
    except Exception as exc:
        logger.error("stats collection access failed: {exc}", exc=exc)
        out['qa_cache_count'] = 0
    if json_out:
        import json
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"Embeddings total: {out.get('total', 0)}")
    for row in out.get("per_model", []):
        print(f"  {row.get('model_id')}: {row.get('count')}")
    print(f"Anchor score cache: {out.get('anchor_cache_count')} entries")
    print(f"QA cache: {out.get('qa_cache_count')} entries")


@app.command("purge")
def purge(
    model_id: str = typer.Option(..., help="Model id to purge from cache"),
    demo_batch: str = typer.Option("", help="Optional lessons.demo_batch filter"),
    all: bool = typer.Option(False, help="Purge all entries for model_id (ignore demo_batch)"),
):
    db = get_db()
    if all:
        aql = "FOR d IN lesson_embeddings FILTER d.model_id==@m REMOVE d IN lesson_embeddings RETURN OLD._key"
        removed = list(db.aql.execute(aql, bind_vars={"m": model_id}))
        print("purged:", len(removed))
        return
    if not demo_batch:
        print("demo_batch required unless --all is set")
        raise typer.Exit(2)
    # Purge by joining with lessons by lesson_id and demo_batch
    aql = """
    FOR e IN lesson_embeddings
      FILTER e.model_id==@m
      LET k = SPLIT(e.lesson_id, '/')[1]
      LET l = DOCUMENT('lessons_v2', k)
      FILTER l != null AND l.demo_batch==@b AND l.demo==true
      REMOVE e IN lesson_embeddings RETURN OLD._key
    """
    removed = list(db.aql.execute(aql, bind_vars={"m": model_id, "b": demo_batch}))
    print("purged:", len(removed))
