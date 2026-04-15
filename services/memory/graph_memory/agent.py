from __future__ import annotations
from typing import Any, Dict

from .lessons.seed import seed as _seed
from .lessons.edges import approve as _approve_cli


def seed(count: int = 50, scope: str = "", batch: str = "") -> None:
    _seed(count=count, scope=scope, batch=batch)


def recall(q: str, scope: str = "", k: int = 5, depth: int = 2,
           use_dense: bool = True, dense_w: float = 0.25, mmr: bool = False) -> Dict[str, Any]:
    from .lessons.recall import bm25_rank, fuse_bm25_graph
    from .arango_client import get_db
    db = get_db()
    bm = bm25_rank(db, q=q, scope=scope, tags=[], k=k)
    fused = fuse_bm25_graph(db, bm25=bm, depth=depth, k=k)
    return {"meta": {"q": q, "scope": scope, "k": k, "depth": depth}, "items": fused}


def related(title: str, scope: str = "") -> Dict[str, Any]:
    from .lessons.related import related as _related
    return _related(title=title, scope=scope)


def multihop(title: str, scope: str = "", depth: int = 2) -> Dict[str, Any]:
    from .lessons.multihop import multihop as _mh
    return _mh(title=title, scope=scope, depth=depth)


def approve_edge(edge_id: str = "", from_title: str = "", to_title: str = "", from_scope: str = "", to_scope: str = "",
                 human_rationale: str = "Approved") -> None:
    _approve_cli(edge_id=edge_id, from_title=from_title, from_scope=from_scope, to_title=to_title, to_scope=to_scope,
                 human_rationale=human_rationale)

__all__ = [
    "seed",
    "recall",
    "related",
    "multihop",
    "approve_edge",
]
