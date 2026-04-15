from __future__ import annotations
import time
from typing import List, Tuple, Dict, Any, Optional
from loguru import logger

from .schema import ActionType, Observation, StepResult, EpisodeConfig, Metrics, EdgeAction, AQLAction, AnchorBoostAction
from .reward import recall_at_k, mrr, ndcg, aggregate_reward
from ..setup_schema import ensure_collections_and_view
from ..arango_client import get_db
from .. import api as gm_api
from .episodes_store import add_episode as _ep_add, end_episode as _ep_end, log_step as _ep_log


class GraphWorld:
    """
    Read-only evaluation world for retrieval metrics over the existing lessons index.
    Actions are simulated/no-ops unless future phases enable apply_writes.
    """

    def __init__(self, cfg: EpisodeConfig):
        self.cfg = cfg
        self._episode_id: Optional[str] = None
        self._step_idx: int = 0

    def __enter__(self):
        ensure_collections_and_view()
        # Optional episodic logging for GraphWorld runs (off unless env enables)
        try:
            import os
            if os.getenv("GRAPHOPS_EPISODES_ENABLE", "0") == "1":
                meta = {
                    "apply_writes": bool(self.cfg.apply_writes),
                    "k": int(self.cfg.k),
                    "as_of": self.cfg.as_of,
                    "within_days": self.cfg.within_days,
                }
                self._episode_id = _ep_add(scope=self.cfg.scope, meta=meta)
        except Exception as exc:
            logger.error("Suppressed error in env: {}", exc)
            # never fail on logging
            self._episode_id = None
        return self

    def __exit__(self, exc_type, exc, tb):
        # best-effort to end episode
        if self._episode_id:
            try:
                _ep_end(self._episode_id, summary={"ok": exc is None})
            except Exception as exc:
                logger.error("Suppressed error in env: {}", exc)
        return False

    # --- core ---
    def _observe(self, q: Optional[str]) -> Observation:
        top_bm25: List[str] = []
        top_vec: List[str] = []
        if q:
            try:
                res = gm_api.search(q, scope=self.cfg.scope or "", k=self.cfg.k)
                items = res.get("items") or []
                ids = [f"lessons/{it.get('_key')}" for it in items if it.get("_key")]
                top_bm25 = ids[:]
            except Exception as exc:
                logger.error("Suppressed error in env: {}", exc)
                top_bm25 = []
        graph_stats = self._graph_stats()
        constraints = {"archived_filter_applied": True}
        return Observation(
            ts=int(time.time()),
            scope=self.cfg.scope,
            q=q,
            top_bm25=top_bm25,
            top_vector=top_vec,
            graph_stats=graph_stats,
            constraints=constraints,
        )

    def _metrics(self, preds: List[str], gold: List[str], took_ms: int) -> Metrics:
        return Metrics(
            recall_at_k=recall_at_k(preds, gold),
            mrr=mrr(preds, gold),
            ndcg=ndcg(preds, gold),
            took_ms=took_ms,
        )

    def step(self, action, q_gold: Tuple[str, List[str]]) -> StepResult:
        q, gold = q_gold
        before_obs = self._observe(q)
        t0 = time.time()
        violations: List[str] = []
        errors: List[str] = []

        # Simulate action (no writes in MVP)
        try:
            if getattr(action, "type", None) == ActionType.ANCHOR_BOOST:
                pass  # no-op boost in MVP
            elif getattr(action, "type", None) in (ActionType.PROPOSE_EDGE, ActionType.DELETE_EDGE, ActionType.INVALIDATE, ActionType.AQL):
                # Reads-only world: do not apply; mark as no-op
                pass
            else:
                errors.append(f"unknown action: {getattr(action, 'type', None)}")
        except Exception as e:
            errors.append(str(e))

        after_obs = self._observe(q)
        took_ms = int((time.time() - t0) * 1000)
        if took_ms > self.cfg.max_latency_ms:
            violations.append("latency_budget_exceeded")

        before = self._metrics(before_obs.top_bm25, gold, took_ms=0)
        after = self._metrics(after_obs.top_bm25, gold, took_ms=took_ms)

        reward = aggregate_reward(
            before=vars(before),
            after=vars(after),
            weights=self.cfg.weights,
            penalties=self.cfg.penalties,
            latency_ms=took_ms,
            violations=len(violations),
            edge_cost_units=self._edge_cost_units(action),
        )

        # Optional: log this step to episode_steps (never block on failure)
        if self._episode_id:
            try:
                _ep_log(
                    episode_id=self._episode_id,
                    step_idx=self._step_idx,
                    observation={
                        "before": {
                            "q": before_obs.q,
                            "top_bm25": before_obs.top_bm25,
                            "graph_stats": before_obs.graph_stats,
                        },
                        "after": {
                            "q": after_obs.q,
                            "top_bm25": after_obs.top_bm25,
                            "graph_stats": after_obs.graph_stats,
                        },
                    },
                    action={k: v for k, v in (getattr(action, "__dict__", {}) or {}).items() if v is not None},
                    result={
                        "metrics_before": vars(before),
                        "metrics_after": vars(after),
                        "reward": reward,
                        "violations": violations,
                        "errors": errors,
                    },
                    tool_meta={"took_ms": took_ms},
                )
                self._step_idx += 1
            except Exception as exc:
                logger.error("Suppressed error in env: {}", exc)

        return StepResult(
            ok=len(errors) == 0,
            observation=after_obs,
            reward=reward,
            metrics_before=before,
            metrics_after=after,
            violations=violations,
            errors=errors,
        )

    # --- helpers ---
    def _graph_stats(self) -> Dict[str, Any]:
        try:
            db = get_db()
            rows = list(db.aql.execute("RETURN LENGTH(lesson_edges)"))
            return {"edges_total": int(rows[0] or 0)}
        except Exception as exc:
            logger.error("Suppressed error in env: {}", exc)
            return {"edges_total": 0}

    def _edge_cost_units(self, action) -> float:
        t = getattr(action, "type", None)
        if t == ActionType.PROPOSE_EDGE:
            return 1.0
        if t in (ActionType.DELETE_EDGE, ActionType.INVALIDATE):
            return 0.5
        return 0.0
