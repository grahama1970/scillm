from __future__ import annotations
import time
from typing import Any, Dict, List, Optional
from ..arango_client import get_db

def fetch_day_residue(
    db: Any,
    limit: int = 10,
    scopes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch weighted temporal memories to drive dream generation.
    Algorithm:
    - 70% Day Residue (0-48h)
    - 20% Dream Lag (5-7 days)
    - 10% Semantic Resonance (>7 days, similarity-based)
    """
    now = int(time.time())
    day_residue_cutoff = now - (48 * 3600)
    dream_lag_start = now - (7 * 24 * 3600)
    dream_lag_end = now - (5 * 24 * 3600)

    # 1. Fetch Day Residue (0-48h)
    # We mix from agent_conversations, episodes, and lessons
    dr_limit = max(1, int(limit * 0.7))
    
    # Simple AQL to get recent activity
    dr_query = """
    LET convs = (
        FOR m IN agent_conversations
        FILTER m.ts_unix >= @cutoff
        SORT m.ts_unix DESC
        LIMIT @limit
        RETURN { id: m._id, text: m.body, type: 'conversation', ts: m.ts_unix, scope: m.scope }
    )
    LET eps = (
        FOR e IN episodes
        FILTER e.created_at >= @cutoff
        SORT e.created_at DESC
        LIMIT @limit
        RETURN { id: e._id, text: e.title, type: 'episode', ts: e.created_at, scope: e.scope }
    )
    LET less = (
        FOR l IN lessons_v2
        FILTER l.updated_at >= @cutoff
        SORT l.updated_at DESC
        LIMIT @limit
        RETURN { id: l._id, text: l.title, type: 'lesson', ts: l.updated_at, scope: l.scope }
    )
    RETURN SLICE(UNION(convs, eps, less), 0, @limit)
    """
    dr_items = list(db.aql.execute(dr_query, bind_vars={'cutoff': day_residue_cutoff, 'limit': dr_limit}))[0]

    # 2. Fetch Dream Lag (5-7 days)
    dl_limit = max(1, int(limit * 0.2))
    dl_query = """
    LET convs = (
        FOR m IN agent_conversations
        FILTER m.ts_unix >= @start AND m.ts_unix <= @end
        SORT m.ts_unix DESC
        LIMIT @limit
        RETURN { id: m._id, text: m.body, type: 'conversation', ts: m.ts_unix, scope: m.scope }
    )
    LET eps = (
        FOR e IN episodes
        FILTER e.created_at >= @start AND e.created_at <= @end
        SORT e.created_at DESC
        LIMIT @limit
        RETURN { id: e._id, text: e.title, type: 'episode', ts: e.created_at, scope: e.scope }
    )
    LET less = (
        FOR l IN lessons_v2
        FILTER l.updated_at >= @start AND l.updated_at <= @end
        SORT l.updated_at DESC
        LIMIT @limit
        RETURN { id: l._id, text: l.title, type: 'lesson', ts: l.updated_at, scope: l.scope }
    )
    RETURN SLICE(UNION(convs, eps, less), 0, @limit)
    """
    dl_items = list(db.aql.execute(dl_query, bind_vars={'start': dream_lag_start, 'end': dream_lag_end, 'limit': dl_limit}))[0]

    # 3. Semantic Resonance (>7 days)
    # Pick a random keyword from day residue to drive semantic search
    sr_items = []
    if dr_items:
        sr_limit = max(1, int(limit * 0.1))
        # Use a simple search on lessons for now
        # In a real implementation, we might use embeddings
        seed_text = dr_items[0]['text']
        sr_query = """
        FOR d IN unified_search
        SEARCH ANALYZER(d.title IN TOKENS(@q, 'text_en') OR d.problem IN TOKENS(@q, 'text_en'), 'text_en')
        FILTER d.updated_at < @cutoff
        SORT BM25(d) DESC
        LIMIT @limit
        RETURN { id: d._id, text: d.title, type: 'resonance', ts: d.updated_at, scope: d.scope }
        """
        sr_items = list(db.aql.execute(sr_query, bind_vars={'q': seed_text, 'cutoff': dream_lag_start, 'limit': sr_limit}))

    # Combine and return
    combined = dr_items + dl_items + sr_items
    return combined
