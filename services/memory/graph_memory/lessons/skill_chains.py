"""Skill chain storage and recall for closed-loop composition learning.

Stores ordered skill chains with performance metrics, taxonomy bridge tags,
and embeddings for semantic recall. Chains are deduplicated by chain_hash
(sha256 of ordered skills + task_type — ordering matters).

Data flows in from:
  - episodic-archiver (automatic, during archive pipeline)
  - bond_harvest nightly (sync from local JSONL state files)
  - manual chain-learn CLI calls

Data flows out via:
  - chain-recall (embedding-based semantic search)
  - skill-selector Phase 2 (future, gated behind 500+ skills)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..arango_client import get_db
from ..embeddings import encode_texts

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLLECTION = "skill_chains"

TASK_TYPES = {
    "extraction": ["extract", "parse", "ingest", "convert", "pdf"],
    "research": ["search", "find", "discover", "arxiv", "paper", "dogpile"],
    "security": ["hack", "scan", "vulnerability", "pentest", "audit"],
    "training": ["train", "fine-tune", "qlora", "lora", "classifier"],
    "creation": ["create", "generate", "build", "scaffold", "write"],
    "monitoring": ["monitor", "watch", "track", "alert", "check"],
    "review": ["review", "assess", "evaluate", "quality", "validate"],
    "memory_op": ["learn", "recall", "remember", "store"],
    "voice": ["voice", "tts", "rvc", "audio", "speech"],
    "media": ["video", "movie", "image", "storyboard", "youtube"],
}

BRIDGE_SIGNALS = {
    "Precision": lambda c: c.get("success_rate", 0) > 0.8 and c.get("avg_latency_ms", 9999) < 5000,
    "Resilience": lambda c: c.get("observation_count", 0) > 5 and c.get("success_rate", 0) > 0.6,
    "Fragility": lambda c: c.get("success_rate", 1) < 0.4,
    "Loyalty": lambda c: c.get("observation_count", 0) > 10,
}

ENERGY_THRESHOLDS = {
    "efficient": (0, 4),
    "moderate": (4, 7),
    "expensive": (7, 10),
    "prohibitive": (10, float("inf")),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_task(description: str) -> str:
    """Classify a task description into a task type via word boundary matching."""
    desc_lower = description.lower()
    scores: dict[str, int] = {}
    for task_type, keywords in TASK_TYPES.items():
        scores[task_type] = sum(
            1 for kw in keywords if re.search(r'\b' + re.escape(kw) + r'\b', desc_lower)
        )
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] > 0 else "general"


def _chain_hash(skills: list[str], task_type: str) -> str:
    """Deterministic hash for deduplication. Preserves skill ordering —
    A→B→C and C→A→B are different chains."""
    payload = json.dumps(skills) + "|" + task_type
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_energy(latency_ms: float, success_rate: float, skill_count: int) -> float:
    """Energy ATP score: lower is better."""
    return math.log2(latency_ms + 1) + (1 - success_rate) * 5 + skill_count * 0.3


def _elegance_grade(energy: float) -> str:
    for grade, (lo, hi) in ENERGY_THRESHOLDS.items():
        if lo <= energy < hi:
            return grade
    return "prohibitive"


def _compute_bridges(doc: dict) -> list[str]:
    return [name for name, fn in BRIDGE_SIGNALS.items() if fn(doc)]


def _compute_adjacencies(skills: list[str]) -> list[str]:
    """Return adjacent skill pairs in the chain. For bond_predictor pair-level
    analysis, NOT for affinity scoring — chain-level success_rate does not
    imply pair-level affinity."""
    return [f"{skills[i]}+{skills[i + 1]}" for i in range(len(skills) - 1)]


def _incremental_update(existing: dict, success: bool, latency_ms: float, now: str) -> dict:
    """Compute updated metrics via Welford's running average.
    Does NOT recompute adjacencies — they're derived from the skill list which never changes."""
    obs = max(existing.get("observation_count", 0), 1)
    old_sr = existing.get("success_rate", 0.5)
    old_lat = existing.get("avg_latency_ms", 0)
    skill_count = len(existing.get("skills", []))

    new_obs = obs + 1
    new_sr = old_sr + (float(success) - old_sr) / new_obs
    new_lat = old_lat + (latency_ms - old_lat) / new_obs if latency_ms > 0 else old_lat

    energy = _compute_energy(new_lat, new_sr, skill_count)
    return {
        "observation_count": new_obs,
        "success_rate": round(new_sr, 4),
        "avg_latency_ms": round(new_lat, 1),
        "energy_atp": round(energy, 2),
        "elegance_grade": _elegance_grade(energy),
        "bridge_tags": _compute_bridges({
            "success_rate": new_sr,
            "avg_latency_ms": new_lat,
            "observation_count": new_obs,
        }),
        "updated_at": now,
    }


def _extract_mind_tags(text: str) -> list[str]:
    """Extract mind tags via /taxonomy/extract (BM25 against SPARTA controls).

    Mind tags are tactical: Detect/Evade/Exploit/Harden/Isolate/Model/Persist/Restore.

    Gated: only calls /taxonomy/extract if the text contains security-related
    keywords. Otherwise BM25 against SPARTA returns matches for any English
    text (Harden=99.9% is noise, not signal).
    """
    # Gate: is this text security-related?
    text_lower = text.lower()
    security_signals = [
        "secur", "vulnerab", "attack", "threat", "exploit", "detect",
        "harden", "isolat", "cwe-", "capec", "mitre", "nist",
        "breach", "malware", "injection", "authentication", "authori",
        "encrypt", "firewall", "pentest", "audit", "compliance",
        "satellite", "spacecraft", "ground station", "command link",
        "telemetry", "uplink", "downlink",
    ]
    if not any(kw in text_lower for kw in security_signals):
        return []

    try:
        import httpx
    except ImportError:
        return []

    url = "http://127.0.0.1:8601"
    try:
        resp = httpx.post(
            f"{url}/taxonomy/extract",
            json={"text": text[:1000], "collection": "operational"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json().get("mind", [])
    except Exception as exc:
        logger.debug(f"Mind tag extraction failed: {exc}")
    return []


def _ensure_collection(db: Any) -> Any:
    if not db.has_collection(COLLECTION):
        db.create_collection(COLLECTION)
    return db.collection(COLLECTION)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def learn_chain(
    skills: list[str],
    task_description: str,
    source: str = "production",
    success: bool = True,
    latency_ms: float = 0,
    db: Any = None,
    # Self-contained fields — chain IS the recall unit
    problem: str = "",
    solution: str = "",
    grade: str = "",
    commit: str = "",
    files_changed: list[str] | None = None,
    tags: list[str] | None = None,
    scope: str = "",
) -> dict:
    """Store or update a skill chain in /memory.

    Deduplicates via chain_hash. If the chain exists, updates metrics
    incrementally (running average). Embeds on insert (Embedding-at-Insert).

    The chain document is self-contained: problem, solution, skills, grade,
    commit, files_changed, and taxonomy tags. No edges needed — recall
    finds it directly via BM25/semantic/graph traversal.
    """
    if db is None:
        db = get_db()
    col = _ensure_collection(db)

    task_type = classify_task(task_description)
    ch = _chain_hash(skills, task_type)
    key = f"chain_{ch[:16]}"

    existing = None
    try:
        existing = col.get(key)
    except Exception as exc:
        logger.error("learn_chain get failed: {exc}", exc=exc)

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        update = _incremental_update(existing, success, latency_ms, now)
        # Backfill problem/solution/grade if existing doc is missing them
        if problem and not existing.get("problem"):
            update["problem"] = problem[:500]
        if solution and not existing.get("solution"):
            update["solution"] = solution[:1000]
        if grade and not existing.get("grade"):
            update["grade"] = grade
        if commit and not existing.get("commit"):
            update["commit"] = commit
        if files_changed and not existing.get("files_changed"):
            update["files_changed"] = files_changed[:20]
        if tags and not existing.get("tags"):
            update["tags"] = tags
        if scope and not existing.get("scope"):
            update["scope"] = scope
        col.update({"_key": key, **update})
        logger.info(f"Updated chain {key} obs={update['observation_count']} sr={update['success_rate']:.3f}")
        return {**existing, **update}

    # New chain — embed immediately
    embed_text = f"{' → '.join(skills)} for {task_description}"
    try:
        vectors = encode_texts([embed_text], normalize=True)
        embedding = vectors[0] if vectors else []
    except Exception as exc:
        logger.warning(f"Embedding failed, marking pending: {exc}")
        embedding = []

    sr = 1.0 if success else 0.0
    energy = _compute_energy(latency_ms, sr, len(skills))

    # Embed problem+solution if available (richer than just skill names)
    if problem or solution:
        embed_text = f"{problem} {solution} {' → '.join(skills)}"
    # else use the original embed_text from above

    # Extract mind tags for multi-hop graph traversal
    # Heart tags are NOT used on skill chains — they're for persona/emotional
    # content (journals, lore, conversations), not coding workflows.
    mind_tags: list[str] = []
    try:
        taxonomy_text = f"{problem} {solution} {task_description}"
        mind_tags = _extract_mind_tags(taxonomy_text)
    except Exception as exc:
        logger.debug(f"Taxonomy extraction skipped: {exc}")

    doc = {
        "_key": key,
        "skills": skills,
        "skill_count": len(skills),
        "task_description": task_description[:500],
        "task_type": task_type,
        "success_rate": sr,
        "observation_count": 1,
        "avg_latency_ms": round(latency_ms, 1),
        "energy_atp": round(energy, 2),
        "elegance_grade": _elegance_grade(energy),
        "bridge_tags": _compute_bridges({
            "success_rate": sr,
            "avg_latency_ms": latency_ms,
            "observation_count": 1,
        }),
        "adjacencies": _compute_adjacencies(skills),
        "shadow": {"tier": "trace", "confidence": 0.5, "teacher_agreement": None},
        "embedding": embedding,
        "_embedding_pending": len(embedding) == 0,
        "source": source,
        "created_at": now,
        "updated_at": now,
        # Self-contained: chain IS the recall unit
        "problem": problem[:500] if problem else "",
        "solution": solution[:1000] if solution else "",
        "grade": grade,
        "commit": commit,
        "files_changed": (files_changed or [])[:20],
        "tags": tags or [],
        "scope": scope,
        # Taxonomy for multi-hop graph traversal
        "mind": mind_tags,                # tactical: Detect/Harden/Model/...
        "code": [task_type],              # workflow: extraction/review/training/...
    }
    col.insert(doc)
    logger.info(f"Stored new chain {key}: {' → '.join(skills)} [{task_type}] mind={mind_tags}")
    return doc


def recall_chains(
    query: str,
    task_type: Optional[str] = None,
    limit: int = 5,
    min_score: float = 0.4,
    db: Any = None,
) -> list[dict]:
    """Semantic recall of skill chains via embedding cosine similarity."""
    if db is None:
        db = get_db()
    if not db.has_collection(COLLECTION):
        return []

    try:
        vectors = encode_texts([query], normalize=True)
        query_vec = vectors[0] if vectors else None
    except Exception as exc:
        logger.warning(f"Embedding query failed: {exc}")
        query_vec = None

    if not query_vec:
        return []

    # AQL dot-product similarity (both vectors are L2-normalized by encode_texts,
    # so dot product == cosine similarity). Avoids per-row norm recomputation.
    type_filter = "FILTER doc.task_type == @task_type" if task_type else ""
    aql = f"""
    FOR doc IN {COLLECTION}
        FILTER LENGTH(doc.embedding) > 0
        {type_filter}
        LET sim = SUM(
            FOR i IN 0..LENGTH(doc.embedding)-1
                RETURN doc.embedding[i] * @qvec[i]
        )
        FILTER sim >= @min_score
        SORT sim DESC
        LIMIT @limit
        RETURN MERGE(UNSET(doc, "embedding"), {{score: ROUND(sim * 1000) / 1000}})
    """
    bind_vars: dict[str, Any] = {
        "qvec": query_vec,
        "min_score": min_score,
        "limit": limit,
    }
    if task_type:
        bind_vars["task_type"] = task_type
    try:
        cursor = db.aql.execute(aql, bind_vars=bind_vars)
        return list(cursor)
    except Exception as exc:
        logger.error(f"Chain recall AQL failed: {exc}")
        return []


def update_chain_outcome(
    chain_key: str,
    success: bool,
    latency_ms: float = 0,
    db: Any = None,
) -> bool:
    """Update a chain's performance metrics after execution."""
    if db is None:
        db = get_db()
    if not db.has_collection(COLLECTION):
        return False

    col = db.collection(COLLECTION)
    try:
        existing = col.get(chain_key)
    except Exception as exc:
        logger.error("update_chain_outcome get failed: {exc}", exc=exc)
        return False

    if not existing:
        return False

    now = datetime.now(timezone.utc).isoformat()
    update = _incremental_update(existing, success, latency_ms, now)
    col.update({"_key": chain_key, **update})
    return True


def sync_from_jsonl(
    traces_path: Optional[str] = None,
    chains_path: Optional[str] = None,
    labels_path: Optional[str] = None,
    db: Any = None,
) -> int:
    """Bootstrap: import chains from skill-lab JSONL state files.

    Reads execution_traces.jsonl and skill_chains.jsonl, deduplicates via
    chain_hash, and stores with embeddings + taxonomy. labels_path is
    accepted for API compatibility but skipped (pair-level, not chain-level).

    TODO: Batch embedding calls — currently calls learn_chain (and thus
    encode_texts) per line. For 728+ chains this means 728 sequential HTTP
    calls. encode_texts supports batching; a collect-then-batch approach
    would reduce to ~8 calls at 100/batch.
    """
    if db is None:
        db = get_db()
    _ensure_collection(db)

    imported = 0

    # Import from skill_chains.jsonl (request → chain pairs)
    if chains_path:
        p = Path(chains_path)
        if p.exists():
            with open(p) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    skills = entry.get("output", [])
                    task_desc = entry.get("input", "")
                    if len(skills) >= 2 and task_desc:
                        try:
                            learn_chain(skills, task_desc[:500], source="transcript", db=db)
                            imported += 1
                        except Exception as exc:
                            logger.error(f"Skip chain import: {exc}")

    # Import from execution_traces.jsonl (pair-level traces → aggregate by ordered chain)
    # Aggregation key uses the same hash that learn_chain will compute, so we
    # must classify the task description first to get a consistent task_type.
    if traces_path:
        p = Path(traces_path)
        if p.exists():
            chain_agg: dict[str, dict] = {}
            with open(p) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("direction") == "reverse":
                        continue
                    chain = entry.get("chain", [])
                    if len(chain) < 2:
                        continue
                    # Use a tuple key — learn_chain will compute the real hash
                    tup = tuple(chain)
                    if tup not in chain_agg:
                        chain_agg[tup] = {"successes": 0, "total": 0, "latencies": []}
                    chain_agg[tup]["total"] += 1
                    if entry.get("success"):
                        chain_agg[tup]["successes"] += 1
                    lat = entry.get("latency_ms", 0)
                    if lat > 0:
                        chain_agg[tup]["latencies"].append(lat)

            for chain_tuple, agg in chain_agg.items():
                skills_list = list(chain_tuple)
                sr = agg["successes"] / max(agg["total"], 1)
                avg_lat = (
                    sum(agg["latencies"]) / len(agg["latencies"])
                    if agg["latencies"] else 0
                )
                desc = f"Traced chain: {' → '.join(skills_list)}"
                try:
                    learn_chain(
                        skills_list, desc, source="warm_pond",
                        success=sr > 0.5, latency_ms=avg_lat, db=db,
                    )
                    imported += 1
                except Exception as exc:
                    logger.error(f"Skip trace import: {exc}")

    # Import from bond_labels.jsonl (pair-level → not full chains, skip for now)
    # Labels are pair-level (skill_a + skill_b), not ordered chains.
    # They feed bond_predictor directly. Chain-level data comes from traces + chains.

    logger.info(f"Synced {imported} chains from JSONL to /memory")
    return imported


def stats(db: Any = None) -> dict:
    """Return summary statistics for the skill_chains collection."""
    if db is None:
        db = get_db()
    if not db.has_collection(COLLECTION):
        return {"total": 0, "message": "skill_chains collection not found"}

    aql = f"""
    LET docs = (FOR d IN {COLLECTION} RETURN d)
    LET total = LENGTH(docs)
    LET by_type = (
        FOR d IN docs
            COLLECT type = d.task_type WITH COUNT INTO cnt
            RETURN {{type, cnt}}
    )
    LET by_source = (
        FOR d IN docs
            COLLECT src = d.source WITH COUNT INTO cnt
            RETURN {{source: src, cnt}}
    )
    LET avg_sr = AVERAGE(FOR d IN docs RETURN d.success_rate)
    LET avg_obs = AVERAGE(FOR d IN docs RETURN d.observation_count)
    RETURN {{
        total: total,
        by_task_type: by_type,
        by_source: by_source,
        avg_success_rate: avg_sr,
        avg_observations: avg_obs,
    }}
    """
    try:
        result = list(db.aql.execute(aql))
        return result[0] if result else {"total": 0}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# CLI (typer)
# ---------------------------------------------------------------------------

def _cli():
    import typer

    app = typer.Typer(help="Skill chain storage and recall for /memory")

    @app.command()
    def learn(
        skills: str = typer.Option(..., help="Comma-separated skill names"),
        task: str = typer.Option(..., help="Task description"),
        source: str = typer.Option("production", help="Data source"),
        success: bool = typer.Option(True, help="Whether chain succeeded"),
        latency: float = typer.Option(0, help="Latency in ms"),
    ):
        """Learn a skill chain to /memory."""
        skill_list = [s.strip() for s in skills.split(",") if s.strip()]
        if len(skill_list) < 2:
            typer.echo("Need at least 2 skills for a chain", err=True)
            raise typer.Exit(1)
        doc = learn_chain(skill_list, task, source=source, success=success, latency_ms=latency)
        typer.echo(json.dumps({"_key": doc.get("_key"), "skills": doc.get("skills"),
                                "observation_count": doc.get("observation_count"),
                                "success_rate": doc.get("success_rate")}, indent=2))

    @app.command()
    def recall(
        query: str = typer.Argument(..., help="Natural language query"),
        task_type: Optional[str] = typer.Option(None, "--task-type", help="Filter by task type"),
        limit: int = typer.Option(5, help="Max results"),
        output_json: bool = typer.Option(False, "--json", help="JSON output"),
    ):
        """Recall skill chains by semantic similarity."""
        results = recall_chains(query, task_type=task_type, limit=limit)
        if output_json:
            typer.echo(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                typer.echo("No matching chains found.")
                return
            for r in results:
                skills_str = " → ".join(r.get("skills", []))
                sr = r.get("success_rate", 0)
                obs = r.get("observation_count", 0)
                score = r.get("score", 0)
                typer.echo(f"  [{score:.2f}] {skills_str}  (sr={sr:.2f}, obs={obs})")

    @app.command()
    def bootstrap(
        traces_file: Optional[str] = typer.Option(None, help="Path to execution_traces.jsonl"),
        chains_file: Optional[str] = typer.Option(None, help="Path to skill_chains.jsonl"),
        labels_file: Optional[str] = typer.Option(None, help="Path to bond_labels.jsonl"),
    ):
        """Bootstrap skill_chains from local JSONL state files."""
        count = sync_from_jsonl(traces_path=traces_file, chains_path=chains_file, labels_path=labels_file)
        typer.echo(f"Imported {count} chains to /memory")

    @app.command(name="stats")
    def stats_cmd():
        """Show skill_chains collection statistics."""
        result = stats()
        typer.echo(json.dumps(result, indent=2, default=str))

    app()


if __name__ == "__main__":
    _cli()
