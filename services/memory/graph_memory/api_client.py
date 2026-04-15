"""Peer module for MemoryClient class and steering functions.

Extracted from api.py to keep modules under 800 lines.
All public functions are re-exported by api.py so existing
``from graph_memory.api import X`` imports continue to work.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from loguru import logger

from .arango_client import get_db
from .lessons.residue import fetch_day_residue


class MemoryClient:
    """Memory-first client for agents. Query memory BEFORE scanning codebases.

    THE PATTERN (non-negotiable):
        1. Call recall() FIRST when encountering any problem
        2. If found=True -> apply existing solution, DO NOT scan codebase
        3. If found=False -> scan codebase, then call learn() to capture

    Example:
        client = MemoryClient(scope="myproject")

        # ALWAYS START HERE - recall before any codebase scan
        result = client.recall("ImportError when running tests")

        if result["found"]:
            # Apply existing solution - DO NOT scan codebase
            print(result["items"][0]["solution"])
        else:
            # No prior knowledge - proceed with codebase scan
            # After solving, capture the lesson:
            client.learn(
                problem="ImportError when running tests outside venv",
                solution="Activate venv first: source .venv/bin/activate"
            )
    """

    def __init__(self, scope: str = "", k: int = 5):
        self.default_scope = scope
        self.default_k = int(k)

    # =========================================================================
    # MEMORY FIRST - Call recall() BEFORE any codebase scanning
    # =========================================================================

    def recall(
        self,
        q: str,
        scope: str | None = None,
        k: int = 5,
        threshold: float = 0.3,
        collections: List[str] | None = None,
        tags: List[str] | None = None,
        entities: List[str] | None = None,
        crosswalk_methods: List[str] | None = None,
    ) -> Dict[str, Any]:
        """CALL THIS FIRST. Query memory before scanning any codebase.

        This is THE entry point for the Memory First pattern. Call this method
        BEFORE any file reading, grep, or codebase exploration.

        Args:
            q: Problem/error/task description to search for
            scope: Project scope (uses default if not provided)
            k: Number of results to return
            threshold: Minimum relevance score (0-1) to consider a match
            collections: Filter by source collections (comma-separated list)

        Returns:
            {
                "found": bool,           # True if relevant prior knowledge exists
                "should_scan": bool,     # True only if NO relevant matches
                "confidence": float,     # How confident we are in the matches
                "items": [...],          # Matching lessons with problem/solution
                "meta": {...}            # Search metadata
            }

        Example:
            result = client.recall("AQL bind variable error")
            if result["found"]:
                print("Solution:", result["items"][0]["solution"])
            else:
                # Now you may scan the codebase
                pass
        """
        # Import from the core api module (not circular: api imports us, but
        # we only call search at runtime via lazy import)
        from .api import search

        result = search(q=q, scope=scope or self.default_scope, k=k, collections=collections, tags=tags, entities=entities, crosswalk_methods=crosswalk_methods)
        items = result.get("items", [])

        # Capability-aware routing for task-oriented queries
        skill_route = None
        if os.getenv("RECALL_CAPABILITY_ROUTING", "1").lower() not in ("0", "false", "no"):
            from .lessons.capability_routing import detect_task_intent, enrich_with_capabilities
            if detect_task_intent(q):
                try:
                    db = get_db()
                    skill_route = enrich_with_capabilities(items, q, db)
                except Exception as _cap_err:
                    import sys
                    print(f"[capability_routing] error: {_cap_err}", file=sys.stderr)

        # Calculate confidence based on top scores
        top_scores = []
        for item in items[:3]:
            scores = item.get("scores", {})
            has_graph = "graph" in scores and scores["graph"] > 0
            if has_graph:
                # Lessons items: blend BM25 + graph
                combined = (scores.get("bm25", 0) * 0.6) + (scores.get("graph", 0) * 0.4)
            else:
                # Supplemental-only items (sparta_qra, etc.): use BM25 directly
                combined = scores.get("bm25", 0)
            top_scores.append(combined)

        avg_confidence = sum(top_scores) / len(top_scores) if top_scores else 0.0
        found = avg_confidence >= threshold and len(items) > 0

        out = {
            "found": found,
            "should_scan": not found,  # Only scan if nothing found
            "confidence": round(avg_confidence, 3),
            "items": items,
            "meta": {
                **result.get("meta", {}),
                "threshold": threshold,
                "memory_first": True,
            },
            "errors": result.get("errors", []),
        }

        if skill_route:
            out["skill_route"] = skill_route

        return out

    def residue(self, limit: int = 10) -> Dict[str, Any]:
        """Fetch weighted temporal memories (Day Residue, Dream Lag, Semantic Resonance)."""
        db = get_db()
        items = fetch_day_residue(db, limit=limit)
        return {
            "meta": {"limit": limit, "count": len(items)},
            "items": items,
            "errors": []
        }

    def learn(
        self,
        problem: str,
        solution: str,
        scope: str | None = None,
        tags: List[str] | None = None,
        code_symbol: bool = False,
    ) -> Dict[str, Any]:
        """Capture a lesson AFTER solving a problem. Call this after codebase work.

        This completes the Memory First loop:
        1. recall() returned found=False
        2. You scanned codebase and solved the problem
        3. NOW call learn() to capture for future agents

        Args:
            problem: The problem that was encountered
            solution: How it was solved
            scope: Project scope
            tags: Optional tags for categorization

        Returns:
            { "meta": {"ok": True}, "items": [lesson], "errors": [] }
        """
        from .arango_client import get_db
        from .api import _split_bridge_and_tags
        import hashlib

        db = get_db()
        ts = int(time.time())
        scope = scope or self.default_scope

        # Auto-taxonomy extraction (Federated Taxonomy)
        input_tags, bridge_from_tags = _split_bridge_and_tags(tags)

        # Extract bridge attributes from input tags first (fast path)
        bridge_attributes = list(set(bridge_from_tags))
        method = "tags_direct" if bridge_attributes else "none"

        # Try taxonomy extraction for additional attributes
        tax_res: Dict[str, Any] = {}
        try:
            from .lessons.store import extract_bridges_fast
            tax_bridge = extract_bridges_fast(f"{problem}\n\n{solution}")
            if tax_bridge:
                tax_res = {"bridge_attributes": tax_bridge, "method": "extract_bridges_fast"}
                bridge_attributes = list(set(bridge_attributes + tax_bridge))
                method = "tags_plus_taxonomy"
        except Exception as exc:
            logger.error("taxonomy extraction for troubleshoot failed: {}", exc)

        # Create title from problem (first 60 chars)
        title = problem[:60] + ("..." if len(problem) > 60 else "")

        # Tags are free-form labels; bridge_attributes are Federated Taxonomy signals.
        # They are independent fields -- never merge one into the other.
        all_tags = list(set(input_tags))

        doc = {
            "title": title,
            "problem": problem,
            "solution": solution,
            "playbook": f"- {solution}",
            "scope": scope,
            "tags": all_tags,
            "bridge_attributes": bridge_attributes,
            "taxonomy_method": method,
            "taxonomy": tax_res,
            "status": "active",
            "added_by": "agent",
            "created_at": ts,
            "updated_at": ts,
            "problem_hash": hashlib.sha256(problem.encode()).hexdigest()[:16],
        }
        if code_symbol:
            doc["code_symbol"] = True

        from .lessons.store import store_lesson

        result_doc = store_lesson(db, doc)
        result = [result_doc]

        lesson = result[0] if result else {}

        # Create embedding for the lesson (same logic as proposer.py)
        embedding_error = None
        vec = None
        if lesson.get("_key"):
            try:
                from .lessons.proposer import _embed_key, _text_hash, doc_text, l2_normalize

                model_id = os.getenv('EMBEDDING_MODEL') or os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
                lesson_id = f"lessons/{lesson['_key']}"
                embed_key = _embed_key(model_id, lesson_id)
                text = doc_text(lesson)
                content_hash = _text_hash(model_id, text)

                import numpy as np

                # Prefer embedding service over local model (avoids dependency issues)
                vec = None
                from .config import EMBEDDING_SERVICE_URL
                if EMBEDDING_SERVICE_URL:
                    try:
                        import requests as _req
                        resp = _req.post(
                            f"{EMBEDDING_SERVICE_URL.rstrip('/')}/embed",
                            json={"text": text},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            raw_vec = data.get("embedding") or data.get("vector", [])
                            if raw_vec:
                                arr = np.array(raw_vec, dtype="float32").reshape(1, -1)
                                arr = l2_normalize(arr)
                                vec = arr[0].tolist()
                    except Exception as exc:
                        logger.error("remote embedding service failed, falling back to local: {}", exc)

                if vec is None:
                    # Fallback: load model locally
                    from sentence_transformers import SentenceTransformer

                    device = os.getenv('EMBEDDING_DEVICE') or os.getenv('GM_DEVICE') or None
                    if (os.getenv('GM_FORCE_CPU') in ('1', 'true', 'TRUE')) or device == 'cpu':
                        os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
                        device = 'cpu'

                    model = SentenceTransformer(model_id, device=device)
                    embedding = model.encode([text], convert_to_numpy=True, normalize_embeddings=True)
                    embedding = l2_normalize(embedding.astype('float32'))
                    vec = embedding[0].tolist()

                # Store in lesson_embeddings collection
                embed_doc = {
                    "_key": embed_key,
                    "lesson_id": lesson_id,
                    "model_id": model_id,
                    "content_hash": content_hash,
                    "dim": len(vec),
                    "embedding": vec,
                    "updated_at": ts,
                }

                embed_col = db.collection("lesson_embeddings")
                try:
                    embed_col.insert(embed_doc)
                except Exception as exc:
                    logger.error("embedding insert failed (trying update): {}", exc)
                    # Already exists, update it
                    try:
                        embed_col.update(embed_doc)
                    except Exception as update_err:
                        embedding_error = str(update_err)
            except Exception as e:
                # Store succeeds but flag for nightly P28 auto-fix
                embedding_error = str(e)
                _lid = locals().get("lesson_id", f"lessons/{lesson.get('_key', '?')}")
                logger.error("Embedding FAILED for {}: {} -- flagged for retry", _lid, e)
                try:
                    db.aql.execute(
                        "UPDATE {_key: @key} WITH {_embedding_pending: true} IN lessons_v2",
                        bind_vars={"key": lesson["_key"]},
                    )
                except Exception as exc:
                    logger.error("embedding_pending flag update failed: {}", exc)

        # Post-learn enrichment: high-fidelity taxonomy + edge proposal
        enrich_errors: List[str] = []
        lesson_key = lesson.get("_key")
        lesson_id = f"lessons/{lesson_key}" if lesson_key else None

        # 1. Taxonomy extraction (fast keyword-based, no LLM)
        if lesson_key:
            try:
                from .lessons.store import extract_bridges_fast
                hf_bridge = extract_bridges_fast(f"{problem}\n\n{solution}")
                if hf_bridge:
                    existing_bridges = list(lesson.get("bridge_attributes") or [])
                    merged_bridges = list(set(existing_bridges + hf_bridge))
                    db.aql.execute(
                        "UPDATE {_key: @key} WITH {bridge_attributes: @bridge, updated_at: @ts} IN lessons_v2",
                        bind_vars={
                            "key": lesson_key,
                            "bridge": merged_bridges,
                            "ts": int(time.time()),
                        },
                    )
            except Exception as exc:
                enrich_errors.append(f"taxonomy: {exc}")

        # 2. Edge proposal: find similar lessons via embedding and create edges
        if lesson_key and vec:
            try:
                import numpy as np
                similar = list(db.aql.execute("""
                    FOR emb IN lesson_embeddings
                        FILTER emb.lesson_id != @lid
                        LET v = emb.embedding
                        FILTER v != null
                        LET sim = COSINE_SIMILARITY(v, @vec)
                        FILTER sim >= 0.65
                        SORT sim DESC
                        LIMIT 5
                        RETURN {lesson_id: emb.lesson_id, similarity: sim}
                """, bind_vars={"lid": lesson_id, "vec": vec}))

                edge_count = 0
                for hit in similar:
                    target_id = hit["lesson_id"]
                    sim = hit["similarity"]
                    # Create bidirectional "related" edges
                    for frm, to in [(lesson_id, target_id), (target_id, lesson_id)]:
                        try:
                            db.aql.execute(
                                """UPSERT {_from: @f, _to: @t, type: "related"}
                                   INSERT {_from: @f, _to: @t, type: "related", weight: @w,
                                           confidence: @w, approved: true, status: "active",
                                           created_at: @ts, updated_at: @ts}
                                   UPDATE {weight: @w, confidence: @w, updated_at: @ts}
                                   IN lesson_edges""",
                                bind_vars={"f": frm, "t": to, "w": sim, "ts": int(time.time())},
                            )
                            edge_count += 1
                        except Exception as exc:
                            logger.error("edge upsert failed: {}", exc)
            except Exception as exc:
                enrich_errors.append(f"edges: {exc}")

        return {
            "meta": {"ok": True, "action": "learned", "embedding_created": embedding_error is None},
            "items": [{
                "_key": lesson.get("_key"),
                "title": lesson.get("title"),
                "problem": lesson.get("problem"),
                "solution": lesson.get("solution"),
                "scope": lesson.get("scope"),
            }],
            "errors": ([embedding_error] if embedding_error else []) + enrich_errors,
        }

    # =========================================================================
    # Secondary methods - use AFTER solved_before() returns should_scan=True
    # =========================================================================

    def search(self, q: str, scope: str | None = None, k: int | None = None, proved_only: bool = False) -> Dict[str, Any]:
        from .api import search
        result = search(q=q, scope=scope or self.default_scope, k=k or self.default_k)
        if proved_only:
            result["items"] = [
                item for item in result.get("items", [])
                if item.get("proof_id") is not None
            ]
            result["meta"]["proved_only"] = True
        return result

    def remember(self, topic: str, scope: str | None = None, max_results: int = 5, pdf_top: int = 0) -> Dict[str, Any]:
        """Enqueue a background research job to ingest arXiv content."""
        from .arango_client import get_db
        from .setup_schema import ensure_collections_and_view

        ensure_collections_and_view()
        db = get_db()
        ts = int(time.time())
        doc = {
            'q': topic,
            'scope': scope or self.default_scope or 'research',
            'max_results': int(max_results),
            'pdf_top': int(pdf_top),
            'status': 'queued',
            'created_at': ts,
        }
        res = db.collection('research_jobs').insert(doc)
        return {"meta": {"ok": True}, "items": [{"job": f"research_jobs/{res['_key']}"}], "errors": []}

    # Convenience wrappers to keep agent surface minimal
    def explain(self, key: str, q: str | None = None, scope: str | None = None) -> Dict[str, Any]:
        from .api import explain
        return explain(key=key, q=q, scope=scope or self.default_scope)

    def related(self, title: str, scope: str | None = None, k: int | None = None) -> Dict[str, Any]:
        from .api import related
        return related(title=title, scope=scope or self.default_scope, k=(k or 10))

    def multihop(self, title: str, scope: str | None = None, depth: int = 2, limit: int = 10) -> Dict[str, Any]:
        from .api import multihop
        return multihop(title=title, scope=scope or self.default_scope, depth=depth, limit=limit)

    def add_edge(self, from_title: str, to_title: str, type: str, from_scope: str | None = None, to_scope: str | None = None, weight: float = 0.75, rationale: str = "Authored", approved: bool = True) -> Dict[str, Any]:
        from .api import add_edge
        return add_edge(from_title=from_title, to_title=to_title, type=type, from_scope=from_scope or self.default_scope, to_scope=to_scope or self.default_scope, weight=weight, rationale=rationale, approved=approved)

    def log_episode(self, status: str, title: str, scope: str | None = None, user_id: str = "", project_id: str = "", thread_id: str = "", tags: List[str] | None = None, details: str = "", promote_if_novel: bool = False) -> Dict[str, Any]:
        from .api import log_episode
        return log_episode(status=status, title=title, scope=scope or self.default_scope, user_id=user_id, project_id=project_id, thread_id=thread_id, tags=tags, details=details, promote_if_novel=promote_if_novel)

    def feedback(self, lesson_title: str, lesson_scope: str | None = None, helpful: bool = True, note: str = "") -> Dict[str, Any]:
        from .api import feedback
        return feedback(lesson_title=lesson_title, lesson_scope=lesson_scope or self.default_scope, helpful=helpful, note=note)

    # Agent conversations
    def add_message(self, id_from: str, id_to: List[str] | str, body: str, topic: str = "", run_id: str = "", session_id: str = "", priority: str = "normal", action_required: bool | None = None, scope: str | None = None) -> Dict[str, Any]:
        from .api_messaging import add_message
        return add_message(
            id_from=id_from,
            id_to=id_to,
            body=body,
            topic=topic,
            run_id=run_id,
            session_id=session_id,
            priority=priority,
            action_required=action_required,
            scope=scope or "agent_conversations",
        )

    def list_messages(self, id_to: str, topic: str | None = None, since_ts: int | str | None = None, limit: int = 50, offset: int = 0, priority: str | None = None, action_required: bool | None = None, scope: str | None = None) -> Dict[str, Any]:
        from .api_messaging import list_messages
        return list_messages(
            id_to=id_to,
            topic=topic,
            since_ts=since_ts,
            limit=limit,
            offset=offset,
            priority=priority,
            action_required=action_required,
            scope=scope or "agent_conversations",
        )

    def ack_message(self, id: str, agent: str) -> Dict[str, Any]:
        from .api_messaging import ack_message
        return ack_message(id=id, agent=agent)

    def get(self, doc_id: str) -> Dict[str, Any]:
        """Retrieve a document by ID (e.g., 'lessons/abc123' or 'episodes/xyz')."""
        from .arango_client import get_db
        db = get_db()
        full_id = doc_id if "/" in doc_id else f"lessons/{doc_id}"
        try:
            doc = db.document(full_id)
            return {"meta": {"ok": True}, "items": [doc] if doc else [], "errors": []}
        except Exception as exc:
            return {"meta": {"ok": False}, "items": [], "errors": [str(exc)]}

    def assess_provability(self, claim: str, local_only: bool = False) -> Dict[str, Any]:
        """Assess whether a claim is provable using the tiered proof system."""
        import asyncio
        from .integrations.proof_assessment import get_provability_score, assess_provability as _assess

        if local_only:
            return get_provability_score(claim)
        return asyncio.run(_assess(claim))

    def trace(self, q: str, answer: str = "", scope: str | None = None, mode: str = "fast", k: int = 10, depth: int = 3, tags: List[str] | None = None) -> Dict[str, Any]:
        """Trace provenance for a query and optional answer."""
        from .api_scripts import trace_provenance
        return trace_provenance(q=q, answer=answer, scope=scope or self.default_scope, mode=mode, k=k, depth=depth, tags=tags)

    # -------------------------------------------------------------------------
    # Unified Query - THE primary method for agents
    # -------------------------------------------------------------------------

    def query(
        self,
        q: str,
        scope: str | None = None,
        k: int = 10,
        sources: str = "all",
        proved_only: bool = False,
        include_graph: bool = False,
        graph_depth: int = 2,
    ) -> Dict[str, Any]:
        """Unified hybrid search across ALL knowledge sources."""
        from .hybrid_search import unified_hybrid_search
        from .arango_client import get_db

        scope = scope or self.default_scope
        db = get_db()

        # Use the unified hybrid search module
        result = unified_hybrid_search(
            db=db,
            query=q,
            scope=scope,
            k=k,
            sources=sources,
            include_graph=include_graph,
            graph_depth=graph_depth,
        )

        # Filter to proved_only if requested
        if proved_only:
            result["items"] = [
                item for item in result.get("items", [])
                if item.get("proof_id") is not None or item.get("_source") == "lean_theorems"
            ]
            result["meta"]["proved_only"] = True
            result["meta"]["count"] = len(result["items"])

        return result

    # -------------------------------------------------------------------------
    # Codebase operations
    # -------------------------------------------------------------------------

    def codebase_status(self, scope: str | None = None) -> Dict[str, Any]:
        """Check if codebase is indexed in database."""
        from .arango_client import get_db

        scope = scope or self.default_scope
        db = get_db()
        counts: Dict[str, int] = {}

        for collection in ["code_symbols", "doc_chunks", "lean_theorems"]:
            if db.has_collection(collection):
                try:
                    count = next(db.aql.execute(
                        f"FOR d IN {collection} FILTER @scope == '' OR d.scope == @scope COLLECT WITH COUNT INTO c RETURN c",
                        bind_vars={"scope": scope}
                    ), 0)
                    counts[collection] = count
                except Exception as exc:
                    logger.error("count query failed for {}: {}", collection, exc)
                    counts[collection] = 0

        total = sum(counts.values())
        return {
            "scope": scope,
            "indexed": total > 0,
            "counts": counts,
            "total": total
        }

    def codebase_ingest(
        self, code_path: str, scope: str | None = None, debug: bool = False,
        no_lean: bool = False, no_pdf: bool = False
    ) -> Dict[str, Any]:
        """Ingest codebase using curate pipeline (P1-P5)."""
        from pathlib import Path

        scope = scope or self.default_scope or "code"
        path = Path(code_path).resolve()

        if not path.exists():
            return {"error": f"Path not found: {path}"}

        try:
            from .codebase.pipeline.p1_init import run_p1_init
            from .codebase.pipeline.p2_ingest import run_p2_ingest
            from .codebase.pipeline.p3_pdf import run_p3_pdf
            from .codebase.pipeline.p4_lean import run_p4_lean
            from .codebase.pipeline.p5_finalize import run_p5_finalize
            from .codebase.pipeline.types import PhaseStatus
            import json

            # Build cli_overrides for feature flags
            cli_overrides = {}
            if no_lean:
                cli_overrides["lean"] = {"enabled": False}
            if no_pdf:
                cli_overrides["pdf"] = {"enabled": False}

            context, p1_result = run_p1_init(
                code_path=path, scope=scope, debug=debug, dry_run=False,
                cli_overrides=cli_overrides if cli_overrides else None
            )
            if p1_result.status == PhaseStatus.FAILED_SOFT:
                return {"error": "P1 init failed", "details": p1_result.errors}

            p2_result = run_p2_ingest(context)
            context.phase_results["ingest"] = p2_result

            p3_result = run_p3_pdf(context)
            context.phase_results["pdf_pipeline"] = p3_result

            p4_result = run_p4_lean(context)
            context.phase_results["lean_pipeline"] = p4_result

            run_p5_finalize(context)

            summary_file = context.artifacts_path / "run_summary.json"
            return json.loads(summary_file.read_text())

        except Exception as exc:
            return {"error": str(exc)}

    def prove(self, claim: str, scope: str | None = None, assess_only: bool = False) -> Dict[str, Any]:
        """Assess and optionally prove a claim using Lean4."""
        assessment = self.assess_provability(claim)

        if assess_only or not assessment.get("provable"):
            return assessment

        # Queue for proof
        scope = scope or self.default_scope
        try:
            from .arango_client import get_db
            db = get_db()
            doc = {
                "claim": claim,
                "scope": scope,
                "status": "pending",
                "assessment": assessment,
                "created_at": int(time.time()),
            }
            result = db.collection("proof_jobs").insert(doc)
            return {
                "queued": True,
                "job_id": result["_key"],
                "assessment": assessment
            }
        except Exception as exc:
            return {"error": str(exc), "assessment": assessment}


def record_assessment(
    project: str,
    issue: Dict[str, Any],
    research: str = "",
    review_outcome: str = "",
    status: str = "success",
    date: str | None = None
) -> Dict[str, Any]:
    """Record a nightly assessment run result."""
    from .setup_schema import ensure_collections_and_view
    ensure_collections_and_view()
    db = get_db()

    if not date:
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc).date().isoformat()

    doc = {
        "project": project,
        "date": date,
        "issue": issue,
        "research": research,
        "review_outcome": review_outcome,
        "status": status,
        "created_at": int(time.time())
    }

    res = db.collection("nightly_assessments").insert(doc)
    doc["_key"] = res["_key"]
    doc["_id"] = res["_id"]
    return doc


def _get_steering_skill():
    """Helper to lazily import steering skill components."""
    import sys
    from pathlib import Path
    steering_path = Path(os.environ.get("STEERING_SKILL", str(Path(__file__).resolve().parent.parent.parent.parent / "pi-mono" / ".pi" / "skills" / "train-convo-steering")))
    if steering_path.exists() and str(steering_path) not in sys.path:
        sys.path.insert(0, str(steering_path))

    try:
        from train_convo_steering.heuristics import estimate_state_bucket
        from train_convo_steering.policy import choose_preset, preset_by_id
        from train_convo_steering.presets import default_presets
        return {
            "estimate_state_bucket": estimate_state_bucket,
            "choose_preset": choose_preset,
            "PRESETS": default_presets(),
            "preset_by_id": preset_by_id
        }
    except Exception as exc:
        logger.error("steering skill import failed: {}", exc)
        return None


def recall_with_steering(q: str, scope: str = "", user_id: str = "", persona_id: str = "", k: int = 5) -> Dict[str, Any]:
    """Unified recall with adaptive conversation steering."""
    from .arango_client import get_steering_prior
    from .api import search

    # 1. Load user priors from ArangoDB
    prior_doc = get_steering_prior(user_id) if user_id else None
    prior = prior_doc.get("prior") if prior_doc else None

    # 2. Get steering components
    steering = _get_steering_skill()
    if not steering:
        return search(q, scope=scope, k=k)

    # 3. Estimate state and choose preset
    state_bucket = steering["estimate_state_bucket"](q)
    decision = steering["choose_preset"](state_bucket, steering["PRESETS"], prior, persona_id=persona_id)

    # 4. Map preset to recall parameters
    adjusted_k = k
    if decision.preset_id == "deep_dive":
        adjusted_k = max(k, 12)
    elif decision.preset_id == "fast_proceed":
        adjusted_k = max(k // 2, 3)
    elif decision.preset_id == "trust_repair":
        adjusted_k = max(k, 8)

    # 5. Execute search
    res = search(q, scope=scope, k=adjusted_k)

    # 6. Attach steering metadata
    preset = steering["preset_by_id"](steering["PRESETS"], decision.preset_id)
    res["steering"] = {
        "preset_id": decision.preset_id,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "state_bucket": state_bucket,
        "config": {
            "pace": preset.pace,
            "grounding": preset.grounding,
            "initiative": preset.initiative,
            "include_reasoning": decision.preset_id in ("deep_dive", "trust_repair")
        }
    }

    return res
