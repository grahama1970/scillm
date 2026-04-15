"""Export hop-weighted positives + hard negatives for SEAL finetuning.

This implements the `lessons-export-judging` CLI defined in pyproject.

Output JSONL schema (one line per training anchor):
{
  "query": "<source lesson title>",
  "pos_ids": ["lessons/<key>", ...],
  "neg_ids": ["lessons/<key>" | "<pair_id>", ...],
  "scope": "<scope>",
  "rationale": "<llm rationale when available>",
  "source": "edge_pos|edge_neg|hard_neg",
  "hop": 1|2,
  "w": <float weight>,
  "ts": <unix seconds>
}

The exporter is conservative and read-only. Training/eval lives elsewhere.
"""

from __future__ import annotations
from loguru import logger

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Set

import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _ts() -> int:
    return int(time.time())


def _run(db, aql: str, bind: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(db.aql.execute(aql, bind_vars=bind))


@app.command()
def main(
    scope: str = typer.Option(..., help="Scope to filter lessons/edges (required)"),
    out: Path = typer.Option(..., exists=False, dir_okay=False, writable=True, resolve_path=True),
    pos_k: int = typer.Option(2000, help="Cap positives per hop"),
    neg_k: int = typer.Option(2000, help="Cap negatives"),
    min_weight_pos: float = typer.Option(0.55, help="Min edge weight for positives"),
    max_weight_neg: float = typer.Option(0.15, help="Max edge weight for negatives"),
    stale_days_neg: int = typer.Option(30, help="Pending edges older than N days as negatives"),
    two_hop_decay: float = typer.Option(0.7, help="Decay for 2-hop positives"),
    json_out: bool = typer.Option(True, "--json"),
):
    if not scope:
        raise typer.BadParameter("scope is required (e.g., workspace:memory)")

    ensure_collections_and_view()
    db = get_db()
    out.parent.mkdir(parents=True, exist_ok=True)
    now = _ts()
    day = 86400

    # 1-hop positives: approved active edges with strong weight + rationale
    pos1_aql = """
    FOR e IN lesson_edges
      FILTER e.approved==true AND e.status=='active'
      FILTER e.weight != null AND e.weight >= @w
      FILTER e.type IN ['related','solves','duplicates','similar_to']
      FILTER e.llm_rationale != null AND e.llm_rationale != ''
      FILTER e.scope == @scope
      SORT e.updated_at DESC
      LIMIT @k
      RETURN {src: e._from, tgt: e._to, w: e.weight, rat: e.llm_rationale, scope: e.scope}
    """
    pos1 = _run(db, pos1_aql, {"w": float(min_weight_pos), "k": int(pos_k), "scope": scope})

    # 2-hop positives: src -> nbr -> nbr2 via approved edges (decayed)
    pos2_aql = """
    FOR e IN lesson_edges
      FILTER e.approved==true AND e.status=='active'
      FILTER e.weight != null AND e.weight >= @w
      FILTER e.type IN ['related','solves','duplicates','similar_to']
      FILTER e.llm_rationale != null AND e.llm_rationale != ''
      FILTER e.scope == @scope
      LIMIT @k
      LET nbr = e._to
      FOR e2 IN lesson_edges
        FILTER e2.approved==true AND e2.status=='active'
        FILTER e2._from == nbr AND e2._to != e._from
        FILTER e2.weight != null AND e2.weight >= @w
        FILTER e2.type IN ['related','solves','duplicates','similar_to']
        FILTER e2.scope == @scope
        LIMIT 1
        RETURN {src: e._from, tgt: e2._to, w: e.weight * @decay, rat: e.llm_rationale, scope: e.scope}
    """
    pos2 = _run(
        db,
        pos2_aql,
        {
            "w": float(min_weight_pos),
            "k": int(pos_k),
            "scope": scope,
            "decay": float(two_hop_decay),
        },
    )

    # Negatives from rejected_pairs (pair_id only; scope not stored there)
    neg_pairs: Set[str] = set()
    rej_aql = """
    FOR r IN rejected_pairs
      SORT r.last_checked_at DESC
      LIMIT @k
      RETURN r.pair_id
    """
    for pid in _run(db, rej_aql, {"k": int(neg_k)}):
        if pid:
            neg_pairs.add(str(pid))

    # Negatives from weak/pending edges in scope
    neg_edge_aql = """
    FOR e IN lesson_edges
      FILTER e.scope == @scope
      FILTER (e.weight != null AND e.weight <= @w) OR
             (e.status=='pending' AND (DATE_NOW()/1000 - e.created_at) > @age)
      SORT e.updated_at DESC
      LIMIT @k
      RETURN {pid: e.pair_id}
    """
    for row in _run(
        db,
        neg_edge_aql,
        {"w": float(max_weight_neg), "age": int(stale_days_neg) * day, "k": int(neg_k), "scope": scope},
    ):
        pid = row.get("pid")
        if pid:
            neg_pairs.add(str(pid))

    # Hard negatives: same-scope lessons not in positive neighborhood (approx disjoint)
    hard_negs: Set[str] = set()
    try:
        hard_neg_aql = """
        FOR d IN lessons_v2
          FILTER d.scope == @scope
          LIMIT @k
          RETURN d._id
        """
        all_ids = {str(r) for r in _run(db, hard_neg_aql, {"scope": scope, "k": int(neg_k) * 2})}
        pos_ids = {r.get("src") for r in pos1} | {r.get("tgt") for r in pos1} | {r.get("tgt") for r in pos2}
        hard_negs = all_ids - {pid for pid in pos_ids if pid}
    except Exception as exc:
        logger.error("Suppressed error in export_judging: {}", exc)
        hard_negs = set()

    def _titles(ids: List[str]) -> Dict[str, str]:
        if not ids:
            return {}
        rows = _run(db, "FOR i IN @ids LET d = DOCUMENT(i) RETURN {id:i, title:d.title}", {"ids": ids})
        return {r["id"]: r.get("title") or "" for r in rows}

    pos_flat: List[str] = []
    for r in pos1 + pos2:
        if r.get("src"):
            pos_flat.append(r["src"])
        if r.get("tgt"):
            pos_flat.append(r["tgt"])
    titles = _titles(pos_flat)

    lines = 0
    with out.open("w", encoding="utf-8") as f:
        for r in pos1:
            tgt_title = titles.get(r["tgt"], "").strip()
            f.write(
                json.dumps(
                    {
                        "query": titles.get(r["src"], "") or r["src"],
                        "pos_ids": [r["tgt"]],
                        "pos_titles": [tgt_title] if tgt_title else [],
                        "neg_ids": [],
                        "scope": r.get("scope") or scope,
                        "rationale": r.get("rat") or "",
                        "source": "edge_pos",
                        "hop": 1,
                        "w": float(r.get("w") or 1.0),
                        "ts": now,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            lines += 1
        for r in pos2:
            tgt_title = titles.get(r["tgt"], "").strip()
            f.write(
                json.dumps(
                    {
                        "query": titles.get(r["src"], "") or r["src"],
                        "pos_ids": [r["tgt"]],
                        "pos_titles": [tgt_title] if tgt_title else [],
                        "neg_ids": [],
                        "scope": r.get("scope") or scope,
                        "rationale": r.get("rat") or "",
                        "source": "edge_pos",
                        "hop": 2,
                        "w": float(r.get("w") or 1.0),
                        "ts": now,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            lines += 1
        for pid in list(neg_pairs)[: int(neg_k)]:
            f.write(
                json.dumps(
                    {
                        "query": pid,
                        "pos_ids": [],
                        "neg_ids": [pid],
                        "scope": scope,
                        "rationale": "rejected_pair/weak",
                        "source": "edge_neg",
                        "hop": 0,
                        "w": 0.0,
                        "ts": now,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            lines += 1
        for hid in list(hard_negs)[: max(1, int(neg_k) // 2)]:
            f.write(
                json.dumps(
                    {
                        "query": hid,
                        "pos_ids": [],
                        "neg_ids": [hid],
                        "scope": scope,
                        "rationale": "hard_negative_disjoint",
                        "source": "hard_neg",
                        "hop": 0,
                        "w": 0.0,
                        "ts": now,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            lines += 1

    meta = {
        "ok": True,
        "scope": scope,
        "out": str(out),
        "lines": lines,
        "pos1": len(pos1),
        "pos2": len(pos2),
        "neg_pairs": min(len(neg_pairs), int(neg_k)),
        "hard_negs": min(len(hard_negs), max(1, int(neg_k) // 2)),
        "ts": now,
    }
    if json_out:
        print(json.dumps({"meta": meta, "items": [], "errors": []}, ensure_ascii=False))
    else:
        print(meta)


def app_main() -> None:
    app()


if __name__ == "__main__":
    app_main()
