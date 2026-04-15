from __future__ import annotations
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    _p = find_dotenv(usecwd=True)
    load_dotenv(_p or None)
except Exception:
    pass
from loguru import logger
import math
import time
import json
from typing import List, Dict, Any
import os
import typer
from threading import Lock
from contextvars import ContextVar

from ..arango_client import get_db

app = typer.Typer(add_completion=False)


# Mind keywords for query-time tag extraction (compat: was _TACTICAL_KEYWORDS)
_MIND_KEYWORDS = {
    "Detect": ["detect", "monitor", "sensor", "alert", "anomaly", "scan"],
    "Evade": ["evade", "stealth", "bypass", "avoid"],
    "Exploit": ["exploit", "attack", "compromise", "breach", "vulnerability"],
    "Harden": ["harden", "protect", "secure", "defense", "patch", "compliance", "stig"],
    "Isolate": ["isolate", "segment", "quarantine", "sandbox", "boundary"],
    "Model": ["model", "assess", "risk", "threat", "analyze", "inventory"],
    "Persist": ["persist", "maintain", "backup", "recover", "continuity"],
    "Restore": ["restore", "recover", "remediat", "incident", "response"],
}

# Intent keywords for query-time UI interaction tag extraction
_INTENT_KEYWORDS = {
    "Navigate": ["zoom", " pan ", "reset", "recenter", "focus on", "scroll", "select", "click", "find node"],
    "Expand": ["expand", "neighbors", "connections", "hop", "reveal"],
    "Filter": ["filter", "perspective", "show only", "hide", "collapse"],
    "Analyze": ["explain", "what is", "describe", "tell me about", "analyze"],
    "Compare": ["compare", "relationship between", "connects", "difference", "versus"],
    "Trace": ["trace", "follow", "call chain", "data flow", "execution path"],
    "Layout": ["layout", "switch", "organic", "stratified", "clustered", "progressive", "toggle"],
    "Persist": ["bookmark", "save", "remember", "learn back", "store"],
}

def _extract_query_bridges(q: str) -> list:
    """Extract bridge + mind + intent tags from query text for graph boost."""
    tags = []
    try:
        from .store import extract_bridges_fast
        tags = extract_bridges_fast(q)
    except Exception as exc:
        logger.error("_extract_query_bridges: bridge extraction FAILED for query={}: {}", q[:80], exc)
    q_lower = q.lower()
    q_padded = f" {q_lower} "  # pad for word-boundary keywords like " pan "
    for tag, keywords in _MIND_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            tags.append(tag)
    for tag, keywords in _INTENT_KEYWORDS.items():
        if any(kw in q_padded for kw in keywords):
            tags.append(tag)
    return tags


def _norm_opt(val: Any, default: Any) -> Any:
    """Normalize typer OptionInfo to actual value when called programmatically."""
    if hasattr(val, 'default'):
        return val.default if val.default is not ... else default
    return val if val is not None else default


_EMBED_MODEL_CACHE: Dict[str, Any] = {}
_EMBED_MODEL_LOCK = Lock()
_LAST_GRAPH_SCORES: ContextVar[Dict[str, float]] = ContextVar("last_graph_scores", default={})


def get_last_graph_scores() -> Dict[str, float]:
    """Return the most recent graph scores for the current execution context."""
    return dict(_LAST_GRAPH_SCORES.get() or {})


def _get_dense_model(model_id: str, device: str | None) -> Any:
    """Cache SentenceTransformer instances by model/device combo."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("sentence-transformers not available") from exc

    key = f"{model_id}::{device or 'auto'}"
    model = _EMBED_MODEL_CACHE.get(key)
    if model is not None:
        logger.debug("Reusing cached embedding model {} (device={})", model_id, device or "auto")
        return model
    with _EMBED_MODEL_LOCK:
        model = _EMBED_MODEL_CACHE.get(key)
        if model is None:
            logger.info("Loading embedding model {} (device={})...", model_id, device or "auto")
            model = SentenceTransformer(model_id, device=device)
            _EMBED_MODEL_CACHE[key] = model
            logger.info(
                "Embedding model {} ready (device={})",
                model_id,
                getattr(model, "target_device", None) or device or "auto",
            )
    return model


def warm_dense_model(model_id: str | None = None, device: str | None = None, emit_progress: bool = True) -> bool:
    """Preload the embedding model so later recall calls are warm."""
    mid = model_id or os.getenv('EMBEDDING_MODEL') or os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
    dev = device or os.getenv('EMBEDDING_DEVICE') or os.getenv('GM_DEVICE') or None
    try:
        if emit_progress:
            logger.info("Warming embedding model '{}' (device={})", mid, dev or "auto")
        _get_dense_model(mid, dev)
        if emit_progress:
            logger.info("Embedding warm complete for model '{}'", mid)
        return True
    except Exception as exc:
        if emit_progress:
            logger.warning("Failed to warm embedding model '{}': {}", mid, exc)
        return False


def _maybe_dense_scores(db, lessons: List[Dict[str, Any]], q: str, k: int) -> Dict[str, float]:
    """Cosine rerank using INLINE embeddings on documents.

    Reads doc['embedding'] directly — no separate embedding collections.
    Every document MUST have an inline embedding field. If missing, this
    logs an error (not a silent fallback).
    """
    try:
        import numpy as np
    except Exception as exc:
        logger.error("numpy not available — semantic search DISABLED: {}", exc)
        return {}

    # Collect inline embeddings from documents.
    # BM25 queries for SPARTA collections strip embedding from RETURN to avoid
    # payload bloat. Backfill missing embeddings via batch AQL per collection.
    vecs = []
    valid_keys = []
    missing: Dict[str, List[str]] = {}  # collection -> [keys]
    for d in lessons:
        emb = d.get('embedding')
        if emb and isinstance(emb, list) and len(emb) > 10:
            vecs.append(emb)
            valid_keys.append(str(d.get('_key', '')))
        else:
            src = d.get('_source', 'lessons')
            missing.setdefault(src, []).append(str(d.get('_key', '')))

    # Backfill embeddings for docs that BM25 returned without them
    _BACKFILL_COLLECTIONS = frozenset({
        'sparta_qra', 'sparta_controls', 'sparta_url_knowledge', 'controls',
        'lessons', 'lessons_v2', 'technique_knowledge', 'binary_features', 'app_actions',
    })
    if missing and db:
        for coll, keys in missing.items():
            if coll not in _BACKFILL_COLLECTIONS:
                logger.error("Embedding backfill skipped for unknown collection '{}' — add to whitelist", coll)
                continue
            try:
                backfill_aql = "FOR d IN @@coll FILTER d._key IN @keys RETURN {_key: d._key, embedding: d.embedding}"
                backfilled = list(db.aql.execute(backfill_aql, bind_vars={'@coll': coll, 'keys': keys}))
                for bd in backfilled:
                    emb = bd.get('embedding')
                    if emb and isinstance(emb, list) and len(emb) > 10:
                        vecs.append(emb)
                        valid_keys.append(str(bd.get('_key', '')))
                    # else: doc genuinely has no embedding — counted below
                logger.debug("Backfilled {}/{} embeddings from {}", len(backfilled), len(keys), coll)
            except Exception as exc:
                logger.error("Embedding backfill FAILED for collection {}: {}", coll, exc)

    # Count truly missing (not just stripped from BM25 return)
    backfilled_keys = set(valid_keys)
    truly_missing = sum(1 for d in lessons if str(d.get('_key', '')) not in backfilled_keys)
    if truly_missing > 0:
        logger.error(
            "INLINE EMBEDDING MISSING on {}/{} docs after backfill — semantic rerank degraded. "
            "Run /embedding backfill.",
            truly_missing, len(lessons),
        )

    if not vecs:
        logger.error("NO inline embeddings found on ANY doc — semantic search FULLY DISABLED for this query")
        return {}

    # Validate embedding dimensions — all docs must match expected dim
    from ..config import EMBEDDING_DIM
    bad_dim = [i for i, v in enumerate(vecs) if len(v) != EMBEDDING_DIM]
    if bad_dim:
        logger.error(
            "EMBEDDING DIMENSION MISMATCH: {}/{} docs have wrong dim (expected {}). "
            "First bad: idx={} dim={}. Dropping mismatched.",
            len(bad_dim), len(vecs), EMBEDDING_DIM, bad_dim[0], len(vecs[bad_dim[0]]),
        )
        good = [(vecs[i], valid_keys[i]) for i in range(len(vecs)) if i not in set(bad_dim)]
        if not good:
            return {}
        vecs, valid_keys = [g[0] for g in good], [g[1] for g in good]

    emb_matrix = np.array(vecs, dtype='float32')

    # Get query embedding from embedding service
    from ..config import EMBEDDING_SERVICE_URL
    qvec = None
    if EMBEDDING_SERVICE_URL:
        try:
            from graph_memory.http_clients import get_session
            resp = get_session().post(
                f"{EMBEDDING_SERVICE_URL.rstrip('/')}/embed",
                json={"text": q},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                vec = data.get("embedding") or data.get("vector", [])
                if vec:
                    qvec = np.array(vec, dtype='float32').reshape(1, -1)
        except Exception as exc:
            logger.error("Embedding service request FAILED: {}", exc)

    if qvec is None:
        model_id = os.getenv('EMBEDDING_MODEL') or os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
        device = os.getenv('EMBEDDING_DEVICE') or os.getenv('GM_DEVICE') or None
        if (os.getenv('GM_FORCE_CPU') in ('1', 'true', 'TRUE')) or device == 'cpu':
            os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
            device = 'cpu'
        try:
            from .proposer import l2_normalize
            model = _get_dense_model(model_id, device)
            qvec = model.encode([q], convert_to_numpy=True, normalize_embeddings=True)
            qvec = l2_normalize(qvec.astype('float32'))
        except Exception as exc:
            logger.error("Local dense model encoding FAILED — NO semantic signal: {}", exc)
            return {}

    # Validate query vector dimension matches document embeddings
    if qvec.shape[-1] != EMBEDDING_DIM:
        logger.error(
            "QUERY EMBEDDING DIMENSION MISMATCH: got {} expected {}. "
            "Embedding service may be using a different model than stored docs.",
            qvec.shape[-1], EMBEDDING_DIM,
        )
        return {}

    # Cosine similarity
    norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb_normed = emb_matrix / norms
    qnorm = np.linalg.norm(qvec)
    if qnorm <= 0:
        return {}
    qvec_normed = qvec / qnorm
    scores = (emb_normed @ qvec_normed.T).flatten()
    top_k = min(k, len(scores))
    top_indices = np.argsort(scores)[::-1][:top_k]
    out: Dict[str, float] = {}
    for idx in top_indices:
        if scores[idx] > 0:
            out[valid_keys[idx]] = float(scores[idx])
    return out


def bm25_rank(db, q: str, scope: str, tags: List[str], k: int, collections: List[str] | None = None) -> List[Dict[str, Any]]:
    _scope = scope or ""
    _coll_filter = set(collections) if collections else None
    # Scope routing: skip irrelevant views to avoid noise and save time.
    # Empty scope = search everything. "sparta" scope = only sparta collections.
    # Collections filter: when specified, only search those specific collections.
    _search_lessons = (not _scope.startswith("sparta")) and (_coll_filter is None or "lessons" in _coll_filter or "checkpoints" in _coll_filter or "walkthroughs" in _coll_filter)
    _search_sparta = (_scope == "" or _scope.startswith("sparta") or _scope == "sparta") and (_coll_filter is None or any(c.startswith("sparta") for c in (_coll_filter or set())))

    base: List[Dict[str, Any]] = []

    # ── unified_search (lessons_v2 + checkpoints + skill_chains) ──
    if not _search_lessons:
        pass  # skip lessons for sparta-scoped or collections-filtered queries
    else:
        bind = {"q": q, "k": max(1, k), "tags": tags, "scope": _scope}
        # Collection-level AQL filter when caller targets specific collections
        coll_filter_aql = ""
        if _coll_filter:
            coll_names = list(_coll_filter)
            bind["colls"] = coll_names
            coll_filter_aql = "FILTER PARSE_IDENTIFIER(d)['collection'] IN @colls"
        aql = f"""
    FOR d IN unified_search
      SEARCH ANALYZER(
        d.title IN TOKENS(@q, 'text_en') OR
        d.problem IN TOKENS(@q, 'text_en') OR
        d.playbook IN TOKENS(@q, 'text_en') OR
        d.chunks IN TOKENS(@q, 'text_en') OR
        d.tags IN TOKENS(@q, 'text_en') OR
        d.keywords IN TOKENS(@q, 'text_en') OR
        d.topic IN TOKENS(@q, 'text_en') OR
        d.resume IN TOKENS(@q, 'text_en') OR
        d.solution IN TOKENS(@q, 'text_en') OR
        d.task_description IN TOKENS(@q, 'text_en') OR
        d.summary IN TOKENS(@q, 'text_en') OR
        d.skill_name IN TOKENS(@q, 'text_en') OR
        d.pipeline_flow IN TOKENS(@q, 'text_en') OR
        d.gate_logic IN TOKENS(@q, 'text_en')
      , 'text_en')
      {coll_filter_aql}
      FILTER LENGTH(@tags)==0 OR LENGTH(INTERSECTION(d.tags || [], @tags)) > 0
      FILTER @scope=='' OR d.scope==@scope
      SORT BM25(d) DESC, TFIDF(d) DESC
      LIMIT @k
      LET src = PARSE_IDENTIFIER(d)['collection']
      RETURN MERGE(KEEP(d, '_key','title','problem','solution','playbook','scope','tags','cluster_id','updated_at','is_midterm','is_longterm','is_summary','bridge_attributes','taxonomy','intent','embedding','topic','resume','grade','outcome','failures','checkpoint_version','skills_used','git','skill_chain','task_description','skill_name','summary','pipeline_flow','gate_logic','expert_reviewer','standards_alignment','innovations','version','date'), {{ _source: src }})
    """
        base = list(db.aql.execute(aql, bind_vars=bind))
        try:
            if base and (q or '').strip():
                qnorm = str(q)
                prefer = [r for r in base if qnorm in str(r.get('title') or '')]
                if prefer:
                    others = [r for r in base if r not in prefer]
                    base = prefer + others
        except Exception as exc:
            logger.error("title substring priority reordering failed: {}", exc)
        try:
            if os.getenv('RECALL_USE_TEXT_STORE', '1').lower() not in ('0','false','no'):
                taql = """
                FOR t IN lesson_texts_search
                  SEARCH ANALYZER(t.text IN TOKENS(@q, 'text_en'), 'text_en')
                  FILTER @scope=='' OR t.scope==@scope
                  LET key = SPLIT(t.lesson_id,'/')[1]
                  FOR l IN lessons_v2 FILTER l._key == key LIMIT 1
                  FILTER LENGTH(@tags)==0 OR LENGTH(INTERSECTION(l.tags || [], @tags)) > 0
                  SORT BM25(t) DESC, TFIDF(t) DESC
                  LIMIT @k
                  RETURN KEEP(l, '_key','title','problem','solution','playbook','scope','tags','cluster_id','updated_at','is_midterm','is_longterm','is_summary','bridge_attributes','taxonomy','intent','embedding')
                """
                text_hits = list(db.aql.execute(taql, bind_vars={'q': q, 'k': max(1, k), 'scope': _scope, 'tags': tags}))
                if text_hits:
                    byk = {r['_key']: r for r in base}
                    for r in text_hits:
                        if r and r.get('_key') not in byk:
                            base.append(r)
        except Exception as exc:
            logger.error("lesson_texts_search blend failed: {}", exc)

    # ── binary_features_search (binary-explorer scope) ──
    if _scope == "binary-explorer" and (_coll_filter is None or "binary_features" in _coll_filter):
        try:
            bf_aql = """
            FOR d IN binary_features_search
              SEARCH ANALYZER(
                d.label IN TOKENS(@q, 'text_en') OR
                d.name IN TOKENS(@q, 'text_en') OR
                d.description IN TOKENS(@q, 'text_en') OR
                d.category IN TOKENS(@q, 'text_en') OR
                d.tags IN TOKENS(@q, 'text_en')
              , 'text_en')
              SORT BM25(d) DESC
              LIMIT @k
              RETURN {
                _key: d._key,
                _source: 'binary_features',
                problem: d.label || d.name || '',
                solution: d.description || '',
                scope: 'binary-explorer',
                tags: d.tags || [],
                nodeType: d.node_type,
                cluster: d.cluster,
                tier: d.extraction_tier,
                category: d.category,
                scores: { bm25: BM25(d) }
              }
            """
            bf_hits = list(db.aql.execute(bf_aql, bind_vars={
                'q': q, 'k': max(1, k),
            }))
            if bf_hits:
                byk = {r['_key']: r for r in base}
                for r in bf_hits:
                    if r and r.get('_key') not in byk:
                        base.append(r)
        except Exception as exc:
            logger.error("binary_features_search FAILED: {}", exc)

    # ── sparta_unified_search: sparta_qra (218K) + sparta_controls (10.5K) +
    #    sparta_url_knowledge (42K) + controls ──
    if _search_sparta:
        try:
            sparta_aql = """
            FOR d IN sparta_unified_search
              SEARCH ANALYZER(
                BOOST(ANALYZER(d.control_id == @q, 'identity'), 5) OR
                d.question IN TOKENS(@q, 'text_en') OR
                d.answer IN TOKENS(@q, 'text_en') OR
                d.reasoning IN TOKENS(@q, 'text_en') OR
                d.name IN TOKENS(@q, 'text_en') OR
                d.description IN TOKENS(@q, 'text_en') OR
                d.text IN TOKENS(@q, 'text_en') OR
                d.topic IN TOKENS(@q, 'text_en') OR
                d.title IN TOKENS(@q, 'text_en') OR
                d.definition IN TOKENS(@q, 'text_en')
              , 'text_en')
              SORT BM25(d) DESC
              LIMIT @k
              LET src = PARSE_IDENTIFIER(d)['collection']
              RETURN {
                _key: d._key,
                _source: src,
                question: d.question || d.name || d.title || d.topic || '',
                answer: d.answer || d.description || d.definition || d.text || '',
                control_id: d.control_id,
                scope: d.scope,
                tags: d.tags || d.tactical_tags || [],
                mind: d.mind,
                source_framework: d.source_framework,
                scores: { bm25: BM25(d) }
              }
            """
            sparta_hits = list(db.aql.execute(sparta_aql, bind_vars={
                'q': q, 'k': max(1, k),
            }))
            if sparta_hits:
                byk = {r['_key']: r for r in base}
                for r in sparta_hits:
                    if r and r.get('_key') not in byk:
                        base.append(r)
        except Exception as exc:
            logger.error("sparta_unified_search FAILED — SPARTA collections NOT searchable: {}", exc)

    return base

SYNONYMS = {
    'cdp': ['chrome', 'devtools', 'chromium'],
    'puppeteer': ['playwright', 'browserless'],
    'json': ['structured', 'schema'],
    'proxy': ['vite', 'backend', 'target', 'api'],
}


def _expand_queries(q: str, tags: List[str]) -> List[str]:
    if not q:
        return []
    toks = [t.lower() for t in q.split() if t.strip()]
    expansions = set([q])
    for t in toks + [t.lower() for t in tags or []]:
        for s in SYNONYMS.get(t, []):
            expansions.add(q + ' ' + s)
    return list(expansions)[:5]


def _get_float_env(names: List[str], default: float) -> float:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            try:
                return float(v)
            except Exception as exc:
                logger.error("env var {} float parse failed: {}", n, exc)
    return default


RELATION_WEIGHTS = {
    'solves': 1.00,
    'verifies': 0.95,
    'caused_by': 0.85,
    'depends_on': 0.80,
    'uses_tool': 0.70,
    'duplicates': 0.60,
    'similar_to': 0.55,
    'related': 0.50,
}

def _causal_types() -> set[str]:
    base = { 'solves', 'caused_by', 'verifies' }
    extra = os.getenv('REL_CAUSAL_TAGS', '').strip()
    if not extra:
        return base
    parts = [p.strip() for p in extra.replace(';', ',').split(',') if p.strip()]
    return base.union(parts)

def _type_weight_with_boost(rel_type: str) -> float:
    base = RELATION_WEIGHTS.get(rel_type, 0.5)
    try:
        boost = float(os.getenv('REL_CAUSAL_BOOST', '1.0') or '1.0')
    except Exception as exc:
        logger.error("REL_CAUSAL_BOOST parse failed: {}", exc)
        boost = 1.0
    if rel_type in _causal_types() and boost != 1.0:
        return max(0.0, min(1.0, base * boost))
    return base

def _causal_propagation_factor(rel_type: str) -> float:
    """Optional causal propagation: small multiplicative factor per causal edge."""
    try:
        if rel_type in _causal_types() and os.getenv('REL_CAUSAL_PROPAGATE') in ('1','true','TRUE'):
            f = float(os.getenv('REL_CAUSAL_PROP_FACTOR', '1.02') or '1.02')
            return max(1.0, min(1.2, f))
    except Exception as exc:
        logger.error("causal propagation factor parse failed: {}", exc)
    return 1.0


def graph_score_for_seed(db, seed_id: str, depth: int, return_paths: bool = False) -> float | tuple[float, list]:
    """Score a seed node by BFS traversal through lesson_edges AND sparta_relationships."""
    import time as _t
    try:
        as_of = int(os.getenv('RECALL_AS_OF') or '0')
    except Exception as exc:
        logger.error("RECALL_AS_OF parse failed: {}", exc)
        as_of = 0
    try:
        within_days = int(os.getenv('RECALL_WITHIN_DAYS') or '0')
    except Exception as exc:
        logger.error("RECALL_WITHIN_DAYS parse failed: {}", exc)
        within_days = 0
    min_ts = int(_t.time()) - within_days * 86400 if within_days > 0 else 0
    aqlg = """
    FOR v, e, p IN 1..@depth ANY @seed lesson_edges
      OPTIONS { bfs: true, uniqueVertices: 'path' }
      FILTER v._id != @seed
      FILTER @asof==0 OR ((e.valid_from==null OR e.valid_from<=@asof) AND (e.valid_to==null OR e.valid_to>=@asof))
      FILTER @min_ts==0 OR (e.last_verified_at!=null AND e.last_verified_at>=@min_ts)
      LIMIT 50
      RETURN p.edges
    """
    try:
        paths = list(db.aql.execute(aqlg, bind_vars={"seed": seed_id, "depth": depth, "asof": as_of, "min_ts": min_ts}))
    except Exception as exc:
        logger.error("graph_score BFS traversal FAILED for seed={}: {}", seed_id[:50], exc)
        paths = []

    # Also traverse sparta_relationships for SPARTA-sourced docs.
    # sparta_relationships is a document collection (not edge), so we use
    # control_id-based lookup instead of graph traversal syntax.
    _GRAPH_TRAVERSAL_COLLECTIONS = {'sparta_qra', 'sparta_controls', 'sparta_url_knowledge', 'controls', 'lessons', 'lessons_v2', 'binary_features', 'app_actions'}
    seed_coll = seed_id.split("/")[0] if "/" in seed_id else ""
    if seed_coll and seed_coll not in _GRAPH_TRAVERSAL_COLLECTIONS:
        logger.error("graph_score_for_seed: collection '{}' not in whitelist, refusing AQL interpolation", seed_coll)
        if return_paths:
            return 0.0, []
        return 0.0
    if (seed_coll.startswith("sparta") or seed_coll == "controls") and db.has_collection("sparta_relationships"):
        try:
            seed_key = seed_id.split("/")[1] if "/" in seed_id else seed_id
            # Get control_id for this doc
            cid_aql = "FOR d IN @@coll FILTER d._key == @key LIMIT 1 RETURN d.control_id"
            cid_result = list(db.aql.execute(cid_aql, bind_vars={"@coll": seed_coll, "key": seed_key}))
            control_id = cid_result[0] if cid_result else None
            if control_id:
                rel_aql = """
                FOR doc IN sparta_relationships
                    FILTER doc.source_control_id == @cid OR doc.target_control_id == @cid
                    LIMIT 20
                    RETURN [{ weight: doc.weight || 0.7, type: doc.type || 'related',
                              last_verified_at: DATE_NOW() / 1000,
                              decay_policy: 'manual_exempt' }]
                """
                sparta_paths = list(db.aql.execute(rel_aql, bind_vars={"cid": control_id}))
                if sparta_paths:
                    logger.info("sparta_relationships: {} edges for control_id={}", len(sparta_paths), control_id)
                paths.extend(sparta_paths)
            else:
                logger.warning("graph_score: no control_id on {}", seed_id)
        except Exception as exc:
            logger.error("sparta_relationships traversal FAILED for {}: {}", seed_id, exc)

    # app_actions seeds → app_action_edges
    if seed_coll == "app_actions" and db.has_collection("app_action_edges"):
        try:
            seed_key = seed_id.split("/")[1] if "/" in seed_id else seed_id
            edge_aql = """
            FOR doc IN app_action_edges
                FILTER doc._from == @seed_id OR doc._to == @seed_id
                LIMIT 20
                RETURN [{ weight: doc.weight || 0.7, type: doc.type || 'related',
                          last_verified_at: DATE_NOW() / 1000,
                          decay_policy: 'standard' }]
            """
            app_paths = list(db.aql.execute(edge_aql, bind_vars={"seed_id": f"app_actions/{seed_key}"}))
            if app_paths:
                logger.info("app_action_edges: {} edges for {}", len(app_paths), seed_id)
            paths.extend(app_paths)
        except Exception as exc:
            logger.error("app_action_edges traversal FAILED for {}: {}", seed_id, exc)

    # binary_features seeds → binary_feature_edges
    if seed_coll == "binary_features" and db.has_collection("binary_feature_edges"):
        try:
            seed_key = seed_id.split("/")[1] if "/" in seed_id else seed_id
            edge_aql = """
            FOR doc IN binary_feature_edges
                FILTER doc._from == @seed_id OR doc._to == @seed_id
                LIMIT 20
                RETURN [{ weight: doc.weight || 0.7, type: doc.type || 'related',
                          last_verified_at: DATE_NOW() / 1000,
                          decay_policy: 'standard' }]
            """
            bin_paths = list(db.aql.execute(edge_aql, bind_vars={"seed_id": f"binary_features/{seed_key}"}))
            if bin_paths:
                logger.info("binary_feature_edges: {} edges for {}", len(bin_paths), seed_id)
            paths.extend(bin_paths)
        except Exception as exc:
            logger.error("binary_feature_edges traversal FAILED for {}: {}", seed_id, exc)

    best = 0.0
    best_paths: List[List[Dict[str, Any]]] = []
    half_life_days_std = _get_float_env([
        "GRAPH_DECAY_HALF_LIFE_DAYS",
        "RECALL_GRAPH_HALF_LIFE_DAYS",
    ], 90.0)
    half_life_days_exempt = _get_float_env([
        "GRAPH_DECAY_EXEMPT_HALF_LIFE_DAYS",
    ], 365.0)

    use_filter = os.getenv('FILTER_GRAPH') in ('1','true','TRUE')
    min_w = float(os.getenv('FILTER_MIN_EDGE_WEIGHT', '0.0') or '0.0') if use_filter else 0.0

    for edges in paths:
        logsum = 0.0
        used_edges = 0
        for ed in edges or []:
            w = float(ed.get("weight") or 0)
            if use_filter and w < min_w:
                continue
            used_edges += 1
            relw = _type_weight_with_boost(str(ed.get('type') or 'related'))
            _raw_ts = ed.get("last_verified_at") or ed.get("created_at") or 0
            try:
                created = int(_raw_ts) if isinstance(_raw_ts, (int, float)) else int(time.time())
            except (ValueError, TypeError):
                created = int(time.time())
            age_days = max(0.0, (time.time() - created) / 86400.0)
            policy = ed.get("decay_policy") or "standard"
            if policy == "manual_exempt" and age_days <= half_life_days_exempt:
                dw = w
            else:
                hl = half_life_days_exempt if policy == "manual_exempt" else half_life_days_std
                dw = w * (0.5 ** (age_days / hl))
            try:
                gamma = float(os.getenv('RELATION_PENALTY_GAMMA', '0.1') or '0.1')
            except Exception as exc:
                logger.error("RELATION_PENALTY_GAMMA parse failed: {}", exc)
                gamma = 0.1
            pen = float(ed.get('penalty') or 0.0)
            pen = max(0.0, min(1.0, pen))
            damp = max(1e-6, 1.0 - gamma * pen)
            cprop = _causal_propagation_factor(str(ed.get('type') or 'related'))
            dw *= relw * damp * cprop
            dw = max(1e-6, min(1.0, dw))
            logsum += 0.9 * math.log(dw)
        if not edges or used_edges == 0:
            score = 0.0
        else:
            score = math.exp(logsum)
        if score > best:
            best = score
            if return_paths:
                best_paths = [edges or []] if used_edges > 0 else []
    if best > 0:
        logger.debug("graph_score_for_seed({}): best={:.4f} from {} paths", seed_id[:50], best, len(paths))
    if return_paths:
        return best, best_paths
    return best


def batch_graph_scores(db, seeds: list[dict], depth: int, crosswalk_methods: list[str] | None = None) -> dict[str, float]:
    """Batch graph scoring: 2 AQL queries instead of N per-seed queries.

    Args:
        crosswalk_methods: Filter sparta_relationships by method category.
            - 'direct': SPARTA-curated CWE→SPARTA (curated:cwe_class_ids)
            - 'nist_nvd': Heimdall NIST mappings (curated:ISO_27001_References, curated:NIST Rev5 Controls)
            - 'mitre_chain': MITRE chain (derived:CWE→CAPEC→ATT&CK, curated:CAPEC→*)

    Returns dict mapping _key -> graph_score (float).
    """
    if not seeds:
        return {}

    import time as _t

    # Map simplified crosswalk method names to edge method prefixes
    _METHOD_PATTERNS: dict[str, list[str]] = {
        "direct": ["curated:cwe_class_ids"],
        "nist_nvd": ["curated:ISO_27001_References", "curated:NIST Rev5 Controls"],
        "mitre_chain": ["derived:CWE→CAPEC→ATT&CK", "curated:CAPEC→CWE", "curated:CAPEC→ATT&CK"],
    }
    method_filter_values: list[str] = []
    if crosswalk_methods:
        for m in crosswalk_methods:
            method_filter_values.extend(_METHOD_PATTERNS.get(m, []))

    try:
        as_of = int(os.getenv('RECALL_AS_OF') or '0')
    except Exception as exc:
        logger.error("RECALL_AS_OF parse failed: {}", exc)
        as_of = 0
    try:
        within_days = int(os.getenv('RECALL_WITHIN_DAYS') or '0')
    except Exception as exc:
        logger.error("RECALL_WITHIN_DAYS parse failed: {}", exc)
        within_days = 0
    min_ts = int(_t.time()) - within_days * 86400 if within_days > 0 else 0

    # Separate seeds by collection type
    lessons_ids: list[str] = []
    app_action_ids: list[str] = []
    sparta_seeds: list[dict] = []  # {_key, _source}
    _GRAPH_TRAVERSAL_COLLECTIONS = {'sparta_qra', 'sparta_controls', 'sparta_url_knowledge', 'controls', 'lessons', 'lessons_v2', 'binary_features', 'app_actions'}
    binary_feature_ids: list[str] = []

    for s in seeds:
        source_coll = s.get("_source", "lessons")
        if source_coll not in _GRAPH_TRAVERSAL_COLLECTIONS:
            logger.error("batch_graph_scores: collection '{}' not in whitelist, skipping", source_coll)
            continue
        if source_coll == "app_actions":
            app_action_ids.append(f"{source_coll}/{s['_key']}")
            continue
        if source_coll == "binary_features":
            binary_feature_ids.append(f"{source_coll}/{s['_key']}")
            continue
        if source_coll.startswith("sparta") or source_coll == "controls":
            sparta_seeds.append(s)
        else:
            lessons_ids.append(f"{source_coll}/{s['_key']}")

    # -- Query 1: Batch BFS traversal for lessons seeds --
    # Returns {seed_id: [path_edges, ...], ...}
    seed_paths: dict[str, list] = {s["_key"]: [] for s in seeds}

    if lessons_ids:
        batch_bfs_aql = """
        FOR seed_id IN @seed_ids
          FOR v, e, p IN 1..@depth ANY seed_id lesson_edges
            OPTIONS { bfs: true, uniqueVertices: 'path' }
            FILTER v._id != seed_id
            FILTER @asof==0 OR ((e.valid_from==null OR e.valid_from<=@asof) AND (e.valid_to==null OR e.valid_to>=@asof))
            FILTER @min_ts==0 OR (e.last_verified_at!=null AND e.last_verified_at>=@min_ts)
            LIMIT 50
            RETURN { seed: seed_id, edges: p.edges }
        """
        try:
            rows = list(db.aql.execute(batch_bfs_aql, bind_vars={
                "seed_ids": lessons_ids,
                "depth": depth,
                "asof": as_of,
                "min_ts": min_ts,
            }))
            for row in rows:
                seed_full = row["seed"]
                seed_key = seed_full.split("/")[1] if "/" in seed_full else seed_full
                if seed_key in seed_paths:
                    seed_paths[seed_key].append(row["edges"])
        except Exception as exc:
            logger.error("batch_graph_scores BFS traversal FAILED: {}", exc)

    # -- Query 1b: Batch BFS traversal for app_actions seeds --
    if app_action_ids:
        batch_app_actions_bfs_aql = """
        FOR seed_id IN @seed_ids
          FOR v, e, p IN 1..@depth ANY seed_id app_action_edges
            OPTIONS { bfs: true, uniqueVertices: 'path' }
            FILTER v._id != seed_id
            LIMIT 50
            RETURN { seed: seed_id, edges: p.edges }
        """
        try:
            rows = list(db.aql.execute(batch_app_actions_bfs_aql, bind_vars={
                "seed_ids": app_action_ids,
                "depth": depth,
            }))
            for row in rows:
                seed_full = row["seed"]
                seed_key = seed_full.split("/")[1] if "/" in seed_full else seed_full
                if seed_key in seed_paths:
                    seed_paths[seed_key].append(row["edges"])
        except Exception as exc:
            logger.error("batch_graph_scores app_action_edges BFS traversal FAILED: {}", exc)

    # -- Query 2: Batch SPARTA relationships lookup --
    if sparta_seeds and db.has_collection("sparta_relationships"):
        sparta_keys = [s["_key"] for s in sparta_seeds]
        sparta_colls = {s["_key"]: s.get("_source", "sparta_qra") for s in sparta_seeds}

        # 2a: Batch lookup control_ids for all SPARTA seeds
        # Group by collection to minimize queries (usually 1-2 collections)
        coll_groups: dict[str, list[str]] = {}
        for s in sparta_seeds:
            coll = s.get("_source", "sparta_qra")
            coll_groups.setdefault(coll, []).append(s["_key"])

        key_to_cid: dict[str, str] = {}
        for coll, keys in coll_groups.items():
            cid_aql = "FOR d IN @@coll FILTER d._key IN @keys RETURN { key: d._key, cid: d.control_id }"
            try:
                cid_rows = list(db.aql.execute(cid_aql, bind_vars={"@coll": coll, "keys": keys}))
                for row in cid_rows:
                    if row.get("cid"):
                        key_to_cid[row["key"]] = row["cid"]
            except Exception as exc:
                logger.error("batch_graph_scores control_id lookup FAILED for {}: {}", coll, exc)

        # 2b: Batch query sparta_relationships for all control_ids at once
        all_cids = list(set(key_to_cid.values()))
        if all_cids:
            cid_to_keys: dict[str, list[str]] = {}
            for k, cid in key_to_cid.items():
                cid_to_keys.setdefault(cid, []).append(k)

            # Build method filter clause if crosswalk_methods specified
            method_filter_clause = ""
            bind_vars: dict = {"cids": all_cids}
            if method_filter_values:
                method_filter_clause = "FILTER doc.method IN @methods"
                bind_vars["methods"] = method_filter_values

            rel_aql = f"""
            FOR doc IN sparta_relationships
                FILTER doc.source_control_id IN @cids OR doc.target_control_id IN @cids
                {method_filter_clause}
                LIMIT 500
                RETURN {{
                    source_cid: doc.source_control_id,
                    target_cid: doc.target_control_id,
                    weight: doc.weight || 0.7,
                    type: doc.type || 'related'
                }}
            """
            try:
                rel_rows = list(db.aql.execute(rel_aql, bind_vars=bind_vars))
                for row in rel_rows:
                    # Map this relationship back to the seed keys it belongs to
                    matched_cids = []
                    if row["source_cid"] in cid_to_keys:
                        matched_cids.append(row["source_cid"])
                    if row["target_cid"] in cid_to_keys:
                        matched_cids.append(row["target_cid"])
                    for cid in matched_cids:
                        for seed_key in cid_to_keys[cid]:
                            # Wrap as a single-edge path to match graph_score_for_seed format
                            edge = {
                                "weight": row["weight"],
                                "type": row["type"],
                                "last_verified_at": int(_t.time()),
                                "decay_policy": "manual_exempt",
                            }
                            seed_paths[seed_key].append([edge])
                if rel_rows:
                    logger.info("batch_graph_scores: {} sparta_relationships for {} control_ids",
                                len(rel_rows), len(all_cids))
            except Exception as exc:
                logger.error("batch_graph_scores sparta_relationships FAILED: {}", exc)
        else:
            for s in sparta_seeds:
                if s["_key"] not in key_to_cid:
                    logger.warning("batch_graph_scores: no control_id on {}/{}", s.get("_source", "?"), s["_key"])

    # -- app_action_edges batch traversal (uses app_action_ids collected above) --
    if app_action_ids and db.has_collection("app_action_edges"):
        try:
            edge_aql = """
            FOR doc IN app_action_edges
                FILTER doc._from IN @ids OR doc._to IN @ids
                RETURN { from_id: doc._from, to_id: doc._to, weight: doc.weight || 0.7, type: doc.type || 'related' }
            """
            rows = list(db.aql.execute(edge_aql, bind_vars={"ids": app_action_ids}))
            key_set = set(app_action_ids)
            for row in rows:
                for full_id in [row["from_id"], row["to_id"]]:
                    if full_id in key_set:
                        seed_key = full_id.split("/")[1]
                        edge = {"weight": row["weight"], "type": row["type"],
                                "last_verified_at": int(_t.time()), "decay_policy": "standard"}
                        seed_paths[seed_key].append([edge])
            if rows:
                logger.info("batch_graph_scores: {} app_action_edges for {} seeds", len(rows), len(app_action_ids))
        except Exception as exc:
            logger.error("batch_graph_scores app_action_edges FAILED: {}", exc)

    # -- binary_feature_edges batch traversal --
    if binary_feature_ids and db.has_collection("binary_feature_edges"):
        try:
            edge_aql = """
            FOR doc IN binary_feature_edges
                FILTER doc._from IN @ids OR doc._to IN @ids
                RETURN { from_id: doc._from, to_id: doc._to, weight: doc.weight || 0.7, type: doc.type || 'related' }
            """
            rows = list(db.aql.execute(edge_aql, bind_vars={"ids": binary_feature_ids}))
            key_set = set(binary_feature_ids)
            for row in rows:
                for full_id in [row["from_id"], row["to_id"]]:
                    if full_id in key_set:
                        seed_key = full_id.split("/")[1]
                        edge = {"weight": row["weight"], "type": row["type"],
                                "last_verified_at": int(_t.time()), "decay_policy": "standard"}
                        seed_paths[seed_key].append([edge])
            if rows:
                logger.info("batch_graph_scores: {} binary_feature_edges for {} seeds", len(rows), len(binary_feature_ids))
        except Exception as exc:
            logger.error("batch_graph_scores binary_feature_edges FAILED: {}", exc)

    # -- Score all paths using the same logic as graph_score_for_seed --
    half_life_days_std = _get_float_env([
        "GRAPH_DECAY_HALF_LIFE_DAYS",
        "RECALL_GRAPH_HALF_LIFE_DAYS",
    ], 90.0)
    half_life_days_exempt = _get_float_env([
        "GRAPH_DECAY_EXEMPT_HALF_LIFE_DAYS",
    ], 365.0)
    use_filter = os.getenv('FILTER_GRAPH') in ('1', 'true', 'TRUE')
    min_w = float(os.getenv('FILTER_MIN_EDGE_WEIGHT', '0.0') or '0.0') if use_filter else 0.0
    try:
        gamma = float(os.getenv('RELATION_PENALTY_GAMMA', '0.1') or '0.1')
    except Exception as exc:
        logger.error("RELATION_PENALTY_GAMMA parse failed: {}", exc)
        gamma = 0.1

    result: dict[str, float] = {}
    for seed_key, paths in seed_paths.items():
        best = 0.0
        for edges in paths:
            logsum = 0.0
            used_edges = 0
            for ed in edges or []:
                w = float(ed.get("weight") or 0)
                if use_filter and w < min_w:
                    continue
                used_edges += 1
                relw = _type_weight_with_boost(str(ed.get('type') or 'related'))
                _raw_ts = ed.get("last_verified_at") or ed.get("created_at") or 0
                try:
                    created = int(_raw_ts) if isinstance(_raw_ts, (int, float)) else int(time.time())
                except (ValueError, TypeError):
                    created = int(time.time())
                age_days = max(0.0, (time.time() - created) / 86400.0)
                policy = ed.get("decay_policy") or "standard"
                if policy == "manual_exempt" and age_days <= half_life_days_exempt:
                    dw = w
                else:
                    hl = half_life_days_exempt if policy == "manual_exempt" else half_life_days_std
                    dw = w * (0.5 ** (age_days / hl))
                pen = float(ed.get('penalty') or 0.0)
                pen = max(0.0, min(1.0, pen))
                damp = max(1e-6, 1.0 - gamma * pen)
                cprop = _causal_propagation_factor(str(ed.get('type') or 'related'))
                dw *= relw * damp * cprop
                dw = max(1e-6, min(1.0, dw))
                logsum += 0.9 * math.log(dw)
            if not edges or used_edges == 0:
                score = 0.0
            else:
                score = math.exp(logsum)
            if score > best:
                best = score
        result[seed_key] = best
    return result


def fuse_bm25_graph(db, bm25: List[Dict[str, Any]], depth: int, k: int, bm25_w: float | None = None, graph_w: float | None = None, query_bridges: List[str] | None = None, crosswalk_methods: List[str] | None = None) -> List[Dict[str, Any]]:
    if not bm25:
        return []
    n = len(bm25)
    scores = {r["_key"]: {"bm25": (n - idx) / max(1, n), "graph": 0.0} for idx, r in enumerate(bm25)}
    depth = max(1, min(4, depth))
    batch_seeds = [{"_key": r["_key"], "_source": r.get("_source", "lessons")} for r in bm25]
    graph_scores_map = batch_graph_scores(db, batch_seeds, depth, crosswalk_methods=crosswalk_methods)
    for r in bm25:
        scores[r["_key"]]["graph"] = graph_scores_map.get(r["_key"], 0.0)
    gvals = [v["graph"] for v in scores.values()]
    mn, mx = (min(gvals), max(gvals)) if gvals else (0.0, 0.0)
    for key, sc in scores.items():
        g = sc["graph"]
        if mx <= mn:
            # All scores equal: use neutral 0.5 so graph doesn't dominate
            sc["graph"] = 0.5 if g > 0 else 0.0
        else:
            sc["graph"] = (g - mn) / (mx - mn)
    if bm25_w is None:
        bm25_w = _get_float_env(["RECALL_BM25_WEIGHT", "GRAPH_SEMANTIC_WEIGHT"], 0.6)
    if graph_w is None:
        graph_w = _get_float_env(["RECALL_GRAPH_WEIGHT", "GRAPH_HIERARCHY_WEIGHT"], 0.4)
    s = max(1e-6, float(bm25_w) + float(graph_w))
    bm25_w = float(bm25_w) / s
    graph_w = float(graph_w) / s
    distilled_penalty = _get_float_env(["RECALL_DISTILLED_PENALTY"], 0.3)
    bridge_boost = _get_float_env(["RECALL_BRIDGE_BOOST"], 0.1)
    query_bridge_set = set(query_bridges) if query_bridges else set()

    fused = []
    for r in bm25:
        sc = scores[r["_key"]]
        final = bm25_w * sc["bm25"] + graph_w * sc["graph"]
        tags = r.get("tags") or []
        if "distilled" in tags and distilled_penalty > 0:
            final *= (1.0 - distilled_penalty)
        if query_bridge_set and bridge_boost > 0:
            doc_bridges = set(r.get("bridge_attributes") or r.get("bridge_tags") or [])
            doc_bridges |= set(r.get("conceptual_tags") or [])
            doc_bridges |= set(r.get("tactical_tags") or [])
            doc_bridges |= set(r.get("intent") or [])
            overlap = len(doc_bridges & query_bridge_set)
            if overlap > 0:
                final += bridge_boost * (overlap / len(query_bridge_set))
        final = min(1.0, max(0.0, final))
        fused.append((final, r, sc["graph"]))
    fused.sort(key=lambda x: x[0], reverse=True)
    results = [r for _, r, _ in fused[:k]]
    graph_scores = {r["_key"]: g for _, r, g in fused[:k]}
    _LAST_GRAPH_SCORES.set(graph_scores)
    fuse_bm25_graph._last_graph_scores = graph_scores
    return results


def _annotate_contradictions(items: List[Dict[str, Any]], db) -> List[Dict[str, Any]]:
    """Annotate recalled lessons with contradicts edges from hint-sweep."""
    keys = [item["_key"] for item in items if item.get("_key")]
    if not keys:
        return items
    try:
        targets = [f"lessons/{k}" for k in keys]
        edges = list(db.aql.execute("""
            FOR e IN lesson_edges
              FILTER e._to IN @targets
              FILTER e.source == "hint-sweep"
              FILTER e.type == "contradicts"
              RETURN { target: e._to, hint_text: e.hint_text,
                       skill: e.skill_name, rationale: e.llm_rationale }
        """, bind_vars={"targets": targets}))
        if not edges:
            return items
        contradiction_map: Dict[str, list] = {}
        for edge in edges:
            target_key = edge["target"].split("/")[1] if "/" in edge.get("target", "") else ""
            if target_key:
                contradiction_map.setdefault(target_key, []).append(edge)
        for item in items:
            if item.get("_key") in contradiction_map:
                item["_contradictions"] = contradiction_map[item["_key"]]
    except Exception as exc:
        logger.error("contradiction annotation failed: {}", exc)
    return items


@app.command()
def recall(
    q: str = typer.Option(..., help="Search query"),
    scope: str = typer.Option("", help="Optional scope filter"),
    tags: str = typer.Option("", help="Optional tags (comma)"),
    k: int = typer.Option(5, help="Top K"),
    depth: int = typer.Option(2, help="Graph depth (1..4)"),
    json_out: bool = typer.Option(False, "--json", help="Output JSON"),
    bm25_w: float = typer.Option(None, help="BM25 weight (overrides env)"),
    graph_w: float = typer.Option(None, help="Graph weight (overrides env)"),
    dense_w: float = typer.Option(0.25, help="Dense weight when blending"),
    dense_k: int = typer.Option(50, help="Top-K for dense KNN search"),
    mmr: bool = typer.Option(False, help="Diversity (MMR) by tags/title overlap"),
    multi_query: bool = typer.Option(False, help="Use simple multi-query expansion"),
):
    q = _norm_opt(q, "")
    scope = _norm_opt(scope, "")
    tags = _norm_opt(tags, "")
    k = _norm_opt(k, 5)
    depth = _norm_opt(depth, 2)
    json_out = _norm_opt(json_out, False)
    bm25_w = _norm_opt(bm25_w, None)
    graph_w = _norm_opt(graph_w, None)
    dense_w = _norm_opt(dense_w, 0.25)
    dense_k = _norm_opt(dense_k, 50)
    mmr = _norm_opt(mmr, False)
    multi_query = _norm_opt(multi_query, False)

    db = get_db()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if multi_query or os.getenv('RECALL_MULTI_QUERY') in ('1','true','TRUE'):
        queries = _expand_queries(q, tag_list)
        cand: Dict[str, Dict[str, Any]] = {}
        for qi in queries or [q]:
            for r in bm25_rank(db, q=qi, scope=scope, tags=tag_list, k=k):
                cand[r['_key']] = r
        bm25 = list(cand.values())[:max(k, len(cand))]
    else:
        bm25 = bm25_rank(db, q=q, scope=scope, tags=tag_list, k=k)
    _qb = _extract_query_bridges(q)
    out = fuse_bm25_graph(db, bm25=bm25, depth=depth, k=k, bm25_w=bm25_w, graph_w=graph_w, query_bridges=_qb)   # graph fusion always on

    if out:
        lang_tokens = set([tok.lower() for tok in q.split() if tok])
        n = len(out)
        for idx, r in enumerate(out):
            base = (n - idx) / max(1, n - 1)
            lang = (r.get('language') or '').lower()
            lang_boost = 0.0
            if lang and lang in lang_tokens:
                lang_boost = 0.05
            r['_lang_score'] = base + lang_boost
        out.sort(key=lambda x: x.get('_lang_score', 0.0), reverse=True)
        for r in out:
            r.pop('_lang_score', None)
    if out:  # dense scoring always on
        dense_scores = _maybe_dense_scores(db, lessons=out, q=q, k=max(k, dense_k))
        if dense_scores:
            vals = list(dense_scores.values())
            mn, mx = (min(vals), max(vals)) if vals else (0.0, 0.0)
            def nz(v):
                return 0.0 if mx <= mn else (v - mn) / (mx - mn)
            for r in out:
                rk = r['_key']
                r['_dense'] = nz(dense_scores.get(str(rk), 0.0))
            n = len(out)
            for i, r in enumerate(out):
                r['_rr'] = (n - i) / max(1, n - 1)
                r['_final'] = (1.0 - float(dense_w)) * r['_rr'] + float(dense_w) * r.get('_dense', 0.0)
            out.sort(key=lambda x: x.get('_final', 0.0), reverse=True)
            for r in out:
                for f in ('_dense','_rr','_final'):
                    if f in r:
                        del r[f]
    if mmr and out:
        sel: List[Dict[str, Any]] = []
        cand = out[:]
        def sim(a, b):
            at = set((a.get('title') or '').lower().split())
            bt = set((b.get('title') or '').lower().split())
            tovl = len(at & bt) / max(1, len(at | bt))
            atags = set([t.lower() for t in a.get('tags') or []])
            btags = set([t.lower() for t in b.get('tags') or []])
            tag = len(atags & btags) / max(1, len(atags | btags))
            return 0.5 * tovl + 0.5 * tag
        lam = 0.7
        while cand and len(sel) < k:
            if not sel:
                sel.append(cand.pop(0))
                continue
            best = None
            best_score = -1
            for r in cand:
                rel = 1.0  # already top-ranked by fused or dense blend
                div = max(sim(r, s) for s in sel)
                score = lam * rel - (1 - lam) * div
                if score > best_score:
                    best_score = score
                    best = r
            sel.append(best)
            cand.remove(best)
        out = sel
    if os.getenv('RECALL_USE_RERANKER') in ('1','true','TRUE'):
        model_name = os.getenv('RERANKER_MODEL', 'cross-encoder/ms-marco-MiniLM-L-6-v2')
        try:
            from sentence_transformers import CrossEncoder  # type: ignore
            cached = list(db.aql.execute("FOR r IN reranker_cache FILTER r.model==@m AND r.q==@q RETURN r", bind_vars={'m': model_name, 'q': q}))
            cmap = {c.get('lesson_key'): float(c.get('score') or 0.0) for c in cached}
            pairs = []
            idxs = []
            for i, r in enumerate(out):
                if r['_key'] not in cmap:
                    pairs.append((q, r['title'] + '\n' + ' '.join(r.get('tags') or [])))
                    idxs.append(i)
            if pairs:
                rer = CrossEncoder(model_name)
                scores = rer.predict(pairs)
                ts = int(time.time())
                for i, sc in zip(idxs, scores):
                    db.collection('reranker_cache').insert({'model': model_name, 'q': q, 'lesson_key': out[i]['_key'], 'score': float(sc), 'at': ts})
                    cmap[out[i]['_key']] = float(sc)
            for r in out:
                r['_rerank'] = float(cmap.get(r['_key'], 0.0))
            out.sort(key=lambda x: x.get('_rerank', 0.0), reverse=True)
            for r in out:
                r.pop('_rerank', None)
        except Exception as exc:
            logger.error("cross-encoder reranking failed: {}", exc)
    out = _annotate_contradictions(out, db)

    if json_out:
        print(json.dumps(out, ensure_ascii=False))
        raise typer.Exit(0)
    for i, r in enumerate(out, 1):
        print(f"{i}. {r['title']} ({r.get('scope')})  tags={','.join(r.get('tags', []))}")
    return out


diff_app = typer.Typer(add_completion=False)


@diff_app.command("diff")
def diff(
    q: str = typer.Option(..., help="Search query"),
    scope: str = typer.Option("", help="Optional scope filter"),
    tags: str = typer.Option("", help="Optional tags (comma)"),
    k: int = typer.Option(5, help="Top K"),
    depth: int = typer.Option(2, help="Graph depth (1..4)"),
    json_out: bool = typer.Option(False, "--json", help="Output JSON with bm25 and fused"),
    bm25_w: float = typer.Option(None, help="BM25 weight (overrides env)"),
    graph_w: float = typer.Option(None, help="Graph weight (overrides env)"),
):
    q = _norm_opt(q, "")
    scope = _norm_opt(scope, "")
    tags = _norm_opt(tags, "")
    k = _norm_opt(k, 5)
    depth = _norm_opt(depth, 2)
    json_out = _norm_opt(json_out, False)
    bm25_w = _norm_opt(bm25_w, None)
    graph_w = _norm_opt(graph_w, None)

    db = get_db()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    bm25 = bm25_rank(db, q=q, scope=scope, tags=tag_list, k=k)
    _qb = _extract_query_bridges(q)
    fused = fuse_bm25_graph(db, bm25=bm25, depth=depth, k=k, bm25_w=bm25_w, graph_w=graph_w, query_bridges=_qb)
    if json_out:
        print(json.dumps({"bm25": bm25, "fused": fused}, ensure_ascii=False))
        return
    print(f"Query: {q} | scope={scope or '(any)'} tags={','.join(tag_list) or '(none)'}")
    print("\nRank  BM25 Title                                 | Fused Title")
    print("----- ------------------------------------------+-------------------------------")
    for i in range(max(len(bm25), len(fused))):
        b = bm25[i]["title"][:42] if i < len(bm25) else ""
        f = fused[i]["title"][:29] if i < len(fused) else ""
        print(f"{i+1:>4}  {b:<42} | {f}")

def _build_query_subgraph(db, seeds: List[Dict[str, Any]], max_edges: int = 500, depth: int = 2, ambiguity_score: float | None = None) -> List[Dict[str, Any]]:
    """Build a subgraph around seed lessons for diagnostics or advanced scoring."""
    if not seeds:
        return []
    if ambiguity_score is not None:
        if ambiguity_score < 0.6:
            return []
        elif ambiguity_score <= 0.9:
            depth = 1
            max_edges = 3 * len(seeds)
        else:
            depth = 2
            max_edges = 5 * len(seeds)

    seed_ids = [f"lessons/{s['_key']}" for s in seeds]
    aql = """
    LET seeds = @seeds
    FOR s IN seeds
      FOR v, e, p IN 1..@depth ANY s lesson_edges
        FILTER v._id != s
        FILTER @asof==0 OR ((e.valid_from==null OR e.valid_from<=@asof) AND (e.valid_to==null OR e.valid_to>=@asof))
        FILTER @min_ts==0 OR (e.last_verified_at!=null AND e.last_verified_at>=@min_ts)
        LIMIT @limit
        RETURN { seed: s, v: v, e: p.edges }
    """
    import time as _t
    try:
        as_of = int(os.getenv('RECALL_AS_OF') or '0')
    except Exception as exc:
        logger.error("RECALL_AS_OF parse failed in subgraph: {}", exc)
        as_of = 0
    try:
        within_days = int(os.getenv('RECALL_WITHIN_DAYS') or '0')
    except Exception as exc:
        logger.error("RECALL_WITHIN_DAYS parse failed in subgraph: {}", exc)
        within_days = 0
    min_ts = int(_t.time()) - within_days * 86400 if within_days > 0 else 0
    rows = list(db.aql.execute(aql, bind_vars={"seeds": seed_ids, "depth": max(1, min(4, depth)), "limit": max(1, int(max_edges/len(seed_ids) if seed_ids else max_edges)), "asof": as_of, "min_ts": min_ts}))
    return rows


def _score_subgraph_nodes(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Score nodes in a subgraph by relation type weights and path length decay."""
    if not rows:
        return {}
    import json as _json
    try:
        tw = _json.loads(os.getenv('RELATION_TYPE_WEIGHTS', '') or 'null') or {}
    except Exception as exc:
        logger.error("RELATION_TYPE_WEIGHTS JSON parse failed: {}", exc)
        tw = {}
    type_w = {**RELATION_WEIGHTS, **{str(k): float(v) for k, v in tw.items() if isinstance(v, (int, float))}}
    try:
        cboost = float(os.getenv('REL_CAUSAL_BOOST', '1.0') or '1.0')
    except Exception as exc:
        logger.error("REL_CAUSAL_BOOST parse failed in subgraph scoring: {}", exc)
        cboost = 1.0
    if cboost != 1.0:
        for k in list(type_w.keys()):
            if k in _causal_types():
                type_w[k] = max(0.0, min(1.0, type_w[k] * cboost))
    decay = float(os.getenv('PATH_DECAY', '0.85') or '0.85')
    scores: Dict[str, float] = {}
    for row in rows:
        v = row.get('v') or {}
        key = v.get('_key') or (v.get('_id','').split('/')[-1] if v.get('_id') else None)
        if not key:
            continue
        edges = row.get('e') or []
        pathlen = max(1, len(edges))
        relprod = 1.0
        for ed in edges:
            relprod *= type_w.get(str(ed.get('type') or 'related'), 0.5)
        score = relprod * (decay ** (pathlen - 1))
        scores[key] = scores.get(key, 0.0) + score
    return scores
