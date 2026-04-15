"""SPARTA pipeline read/query operations over ArangoDB.

Split from sparta_data_bridge.py — all read, count, sample, and diagnostic
methods live here.  SpartaDataQueries inherits from SpartaDataBridgeBase so
callers get both write and read capabilities in one object.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from .sparta_data_bridge import COLLECTIONS, SpartaDataBridgeBase


class SpartaDataQueries(SpartaDataBridgeBase):
    """Read/query layer on top of the SPARTA write bridge."""

    # ------------------------------------------------------------------
    # Readers — counts
    # ------------------------------------------------------------------

    def _count(self, logical: str) -> int:
        coll = COLLECTIONS[logical]
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@coll RETURN 1)",
                bind_vars={"@coll": coll},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count({}) failed: {}", logical, exc)
            return 0

    def count_controls(self) -> int:
        return self._count("controls")

    def count_urls(self) -> int:
        return self._count("urls")

    def count_relationships(self) -> int:
        return self._count("relationships")

    def count_knowledge_anchors(self) -> int:
        return self._count("knowledge_anchors")

    def count_url_knowledge(self) -> int:
        return self._count("url_knowledge")

    def count_url_content(self) -> int:
        return self._count("url_content")

    def count_brandon_reviews(self) -> int:
        return self._count("brandon_reviews")

    # ------------------------------------------------------------------
    # Readers — controls
    # ------------------------------------------------------------------

    def get_all_controls(self) -> list[dict]:
        """Return all controls as dicts."""
        coll = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                "FOR d IN @@coll SORT d.control_id RETURN d",
                bind_vars={"@coll": coll},
            )
            return [self._strip_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("get_all_controls failed: {}", exc)
            return []

    def get_control(self, control_id: str) -> Optional[dict]:
        """Get a single control by control_id."""
        coll = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                "FOR d IN @@coll FILTER d.control_id == @cid LIMIT 1 RETURN d",
                bind_vars={"@coll": coll, "cid": control_id},
            )
            results = list(cursor)
            return self._strip_meta(results[0]) if results else None
        except Exception as exc:
            logger.error("get_control failed: {}", exc)
            return None

    def get_controls_by_type(self, control_type: str) -> list[dict]:
        """Get controls filtered by control_type."""
        coll = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                "FOR d IN @@coll FILTER d.control_type == @ct RETURN d",
                bind_vars={"@coll": coll, "ct": control_type},
            )
            return [self._strip_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("get_controls_by_type failed: {}", exc)
            return []

    def get_tactic_controls(self) -> list[dict]:
        """Get all controls where control_type = 'tactic'."""
        return self.get_controls_by_type("tactic")

    # ------------------------------------------------------------------
    # Readers — source_fidelity (Dim 1)
    # ------------------------------------------------------------------

    def get_controls_as_dict(self) -> dict[str, dict]:
        """Return controls indexed by control_id (for source_fidelity comparison)."""
        rows = self.get_all_controls()
        return {
            r["control_id"]: {
                "name": r.get("name"),
                "description": (r.get("description") or "")[:500] if r.get("description") else None,
                "source_framework": r.get("source_framework"),
                "source_table": r.get("source_table"),
                "control_type": r.get("control_type"),
                "parent_id": r.get("parent_id"),
            }
            for r in rows if r.get("control_id")
        }

    # ------------------------------------------------------------------
    # Readers — reference_coverage (Dim 2)
    # ------------------------------------------------------------------

    def count_controls_with_urls(self) -> int:
        """Count distinct control_ids that have at least one URL."""
        try:
            cursor = self.db.aql.execute(
                """RETURN LENGTH(
                    FOR d IN @@coll
                        COLLECT cid = d.control_id
                        RETURN 1
                )""",
                bind_vars={"@coll": COLLECTIONS["control_urls"]},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_controls_with_urls failed: {}", exc)
            return 0

    def get_url_accessibility_stats(self) -> dict:
        """URL fetch stats: accessible, broken, no_content, not_fetched."""
        uc = COLLECTIONS["url_content"]
        u = COLLECTIONS["urls"]
        try:
            cursor = self.db.aql.execute(
                """LET accessible = LENGTH(FOR d IN @@uc FILTER d.status_code >= 200 AND d.status_code <= 299 RETURN 1)
                   LET broken = LENGTH(FOR d IN @@uc FILTER d.status_code != null AND (d.status_code < 200 OR d.status_code > 299) RETURN 1)
                   LET no_content = LENGTH(FOR d IN @@uc FILTER d.file_path == null AND d.status_code >= 200 AND d.status_code <= 299 RETURN 1)
                   LET fetched_ids = (FOR d IN @@uc RETURN d.url_id)
                   LET not_fetched = LENGTH(FOR d IN @@u FILTER d.url_id NOT IN fetched_ids RETURN 1)
                   RETURN {accessible, broken, no_content, not_fetched}""",
                bind_vars={"@uc": uc, "@u": u},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("get_url_accessibility_stats failed: {}", exc)
            return {"accessible": 0, "broken": 0, "no_content": 0, "not_fetched": 0}

    def get_broken_urls(self, limit: int = 200) -> list[dict]:
        """Get URLs with non-2xx status codes, joined with control info."""
        uc = COLLECTIONS["url_content"]
        u = COLLECTIONS["urls"]
        cu = COLLECTIONS["control_urls"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@uc
                    FILTER d.status_code != null AND (d.status_code < 200 OR d.status_code > 299)
                    LET url_doc = FIRST(FOR u IN @@u FILTER u.url_id == d.url_id RETURN u)
                    LET ctrl = FIRST(FOR c IN @@cu FILTER c.url_id == d.url_id RETURN c.control_id)
                    LIMIT @lim
                    RETURN {
                        url: url_doc.url, status_code: d.status_code,
                        error_message: d.error_message, control_id: ctrl
                    }""",
                bind_vars={"@uc": uc, "@u": u, "@cu": cu, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_broken_urls failed: {}", exc)
            return []

    def get_empty_extractions(self, limit: int = 100) -> list[dict]:
        """Get url_extraction_log entries with source_quality in ('empty','error')."""
        el = COLLECTIONS["url_extraction_log"]
        u = COLLECTIONS["urls"]
        cu = COLLECTIONS["control_urls"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@el
                    FILTER d.source_quality IN ['empty', 'error']
                    LET url_doc = FIRST(FOR u IN @@u FILTER u.url_id == d.url_id RETURN u)
                    LET ctrl = FIRST(FOR c IN @@cu FILTER c.url_id == d.url_id RETURN c.control_id)
                    LIMIT @lim
                    RETURN {
                        url: url_doc.url, source_quality: d.source_quality,
                        error: d.error, excerpt_count: d.excerpt_count,
                        control_id: ctrl
                    }""",
                bind_vars={"@el": el, "@u": u, "@cu": cu, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_empty_extractions failed: {}", exc)
            return []

    def count_empty_extractions(self) -> int:
        el = COLLECTIONS["url_extraction_log"]
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@el FILTER d.source_quality IN ['empty', 'error'] RETURN 1)",
                bind_vars={"@el": el},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_empty_extractions failed: {}", exc)
            return 0

    def count_low_quality_extractions(self) -> int:
        el = COLLECTIONS["url_extraction_log"]
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@el FILTER d.source_quality == 'low' RETURN 1)",
                bind_vars={"@el": el},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_low_quality_extractions failed: {}", exc)
            return 0

    def get_source_quality_breakdown(self) -> dict[str, int]:
        """source_quality -> count from url_extraction_log."""
        el = COLLECTIONS["url_extraction_log"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@el
                    COLLECT sq = d.source_quality WITH COUNT INTO cnt
                    RETURN {sq, cnt}""",
                bind_vars={"@el": el},
            )
            return {row["sq"]: row["cnt"] for row in cursor}
        except Exception as exc:
            logger.error("get_source_quality_breakdown failed: {}", exc)
            return {}

    def get_missing_url_controls(self, limit: int = 200) -> list[dict]:
        """Controls that should have URLs but don't (techniques/countermeasures)."""
        c = COLLECTIONS["controls"]
        cu = COLLECTIONS["control_urls"]
        try:
            cursor = self.db.aql.execute(
                """LET has_urls = (FOR d IN @@cu COLLECT cid = d.control_id RETURN cid)
                   FOR d IN @@c
                       FILTER d.control_type IN ['technique', 'countermeasure']
                           AND d.source_framework IN ['sparta', 'd3fend']
                           AND d.control_id NOT IN has_urls
                       SORT d.control_id
                       LIMIT @lim
                       RETURN {
                           control_id: d.control_id, name: d.name,
                           source_framework: d.source_framework,
                           control_type: d.control_type
                       }""",
                bind_vars={"@c": c, "@cu": cu, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_missing_url_controls failed: {}", exc)
            return []

    def get_controls_no_knowledge(self, limit: int = 200) -> list[dict]:
        """Controls (technique/countermeasure) with URLs but 0 knowledge chunks."""
        c = COLLECTIONS["controls"]
        cu = COLLECTIONS["control_urls"]
        uk = COLLECTIONS["url_knowledge"]
        try:
            cursor = self.db.aql.execute(
                """LET has_knowledge = (
                       FOR k IN @@uk
                           COLLECT uid = k.url_id
                           RETURN uid
                   )
                   FOR ctrl IN @@c
                       FILTER ctrl.control_type IN ['technique', 'countermeasure']
                       LET ctrl_urls = (FOR cu IN @@cu FILTER cu.control_id == ctrl.control_id RETURN cu.url_id)
                       FILTER LENGTH(ctrl_urls) > 0
                       LET has_any = LENGTH(FOR uid IN ctrl_urls FILTER uid IN has_knowledge RETURN 1)
                       FILTER has_any == 0
                       SORT ctrl.control_id
                       LIMIT @lim
                       RETURN {
                           control_id: ctrl.control_id, name: ctrl.name,
                           source_framework: ctrl.source_framework
                       }""",
                bind_vars={"@c": c, "@cu": cu, "@uk": uk, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_controls_no_knowledge failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # Readers — knowledge_quality (Dim 3)
    # ------------------------------------------------------------------

    def count_short_chunks(self, max_len: int = 50) -> int:
        uk = COLLECTIONS["url_knowledge"]
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@uk FILTER LENGTH(d.text || '') < @ml RETURN 1)",
                bind_vars={"@uk": uk, "ml": max_len},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_short_chunks failed: {}", exc)
            return 0

    def count_marginal_chunks(self, min_len: int = 50, max_len: int = 150) -> int:
        uk = COLLECTIONS["url_knowledge"]
        try:
            cursor = self.db.aql.execute(
                """RETURN LENGTH(
                    FOR d IN @@uk
                        LET tl = LENGTH(d.text || '')
                        FILTER tl >= @mn AND tl <= @mx
                        RETURN 1
                )""",
                bind_vars={"@uk": uk, "mn": min_len, "mx": max_len},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_marginal_chunks failed: {}", exc)
            return 0

    def get_short_chunks(self, max_len: int = 50, limit: int = 50) -> list[dict]:
        uk = COLLECTIONS["url_knowledge"]
        u = COLLECTIONS["urls"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@uk
                    FILTER LENGTH(d.text || '') < @ml
                    LET url_doc = FIRST(FOR u IN @@u FILTER u.url_id == d.url_id RETURN u)
                    LIMIT @lim
                    RETURN {
                        url_id: d.url_id, excerpt_index: d.excerpt_index,
                        text: d.text, topic: d.topic, url: url_doc.url
                    }""",
                bind_vars={"@uk": uk, "@u": u, "ml": max_len, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_short_chunks failed: {}", exc)
            return []

    def get_duplicate_chunks(self, limit: int = 50) -> list[dict]:
        """Get duplicate text chunks (exact match)."""
        uk = COLLECTIONS["url_knowledge"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@uk
                    COLLECT t = d.text WITH COUNT INTO cnt
                    FILTER cnt > 1
                    SORT cnt DESC
                    LIMIT @lim
                    RETURN {text: t, count: cnt}""",
                bind_vars={"@uk": uk, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_duplicate_chunks failed: {}", exc)
            return []

    def sample_short_text_chunks(self, max_len: int = 300, limit: int = 1000) -> list[dict]:
        """Sample chunks with short text for boilerplate detection."""
        uk = COLLECTIONS["url_knowledge"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@uk
                    FILTER LENGTH(d.text || '') < @ml
                    LIMIT @lim
                    RETURN {url_id: d.url_id, excerpt_index: d.excerpt_index, text: d.text}""",
                bind_vars={"@uk": uk, "ml": max_len, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("sample_short_text_chunks failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # Readers — prompt_completeness (Dim 4)
    # ------------------------------------------------------------------

    def count_anchors_no_parent_context(self) -> int:
        ka = COLLECTIONS["knowledge_anchors"]
        try:
            cursor = self.db.aql.execute(
                """RETURN LENGTH(
                    FOR d IN @@ka
                        FILTER d.parent_context == null OR LENGTH(d.parent_context || '') < 10
                        RETURN 1
                )""",
                bind_vars={"@ka": ka},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_anchors_no_parent_context failed: {}", exc)
            return 0

    def count_anchors_no_child_context(self) -> int:
        ka = COLLECTIONS["knowledge_anchors"]
        try:
            cursor = self.db.aql.execute(
                """RETURN LENGTH(
                    FOR d IN @@ka
                        FILTER d.child_context == null OR LENGTH(d.child_context || '') < 10
                        RETURN 1
                )""",
                bind_vars={"@ka": ka},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_anchors_no_child_context failed: {}", exc)
            return 0

    def get_anchors_no_parent_context(self, limit: int = 50) -> list[dict]:
        ka = COLLECTIONS["knowledge_anchors"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@ka
                    FILTER d.parent_context == null OR LENGTH(d.parent_context || '') < 10
                    LIMIT @lim
                    RETURN {anchor_id: d.anchor_id, control_id: d.control_id, category: d.category}""",
                bind_vars={"@ka": ka, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_anchors_no_parent_context failed: {}", exc)
            return []

    def count_weak_relationships(self, min_score: float = 0.3) -> int:
        rel = COLLECTIONS["relationships"]
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@rel FILTER d.combined_score < @ms RETURN 1)",
                bind_vars={"@rel": rel, "ms": min_score},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_weak_relationships failed: {}", exc)
            return 0

    def get_weak_relationships(self, min_score: float = 0.3, limit: int = 30) -> list[dict]:
        rel = COLLECTIONS["relationships"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@rel
                    FILTER d.combined_score < @ms
                    SORT d.combined_score ASC
                    LIMIT @lim
                    RETURN {
                        source_control_id: d.source_control_id,
                        target_control_id: d.target_control_id,
                        combined_score: d.combined_score,
                        method: d.method
                    }""",
                bind_vars={"@rel": rel, "ms": min_score, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_weak_relationships failed: {}", exc)
            return []

    def get_isolated_controls(self, limit: int = 100) -> list[dict]:
        """Controls (technique/countermeasure) with 0 relationships."""
        c = COLLECTIONS["controls"]
        rel = COLLECTIONS["relationships"]
        try:
            cursor = self.db.aql.execute(
                """LET in_rels = (FOR r IN @@rel COLLECT cid = r.source_control_id RETURN cid)
                   LET out_rels = (FOR r IN @@rel COLLECT cid = r.target_control_id RETURN cid)
                   LET all_rels = UNION_DISTINCT(in_rels, out_rels)
                   FOR d IN @@c
                       FILTER d.control_type IN ['technique', 'countermeasure']
                           AND d.control_id NOT IN all_rels
                       SORT d.control_id
                       LIMIT @lim
                       RETURN {
                           control_id: d.control_id, name: d.name,
                           source_framework: d.source_framework
                       }""",
                bind_vars={"@c": c, "@rel": rel, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_isolated_controls failed: {}", exc)
            return []

    def count_controls_with_type(self, control_types: list[str]) -> int:
        """Count controls whose control_type is in the given list."""
        c = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@c FILTER d.control_type IN @ct RETURN 1)",
                bind_vars={"@c": c, "ct": control_types},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_controls_with_type failed: {}", exc)
            return 0

    # ------------------------------------------------------------------
    # Readers — unfetched URLs (monitor_sparta)
    # ------------------------------------------------------------------

    def count_unfetched_urls(self) -> int:
        """Count URLs with no content or no status_code."""
        u = COLLECTIONS["urls"]
        uc = COLLECTIONS["url_content"]
        try:
            cursor = self.db.aql.execute(
                """LET fetched = (FOR d IN @@uc FILTER d.status_code != null RETURN d.url_id)
                   RETURN LENGTH(FOR d IN @@u FILTER d.url_id NOT IN fetched RETURN 1)""",
                bind_vars={"@u": u, "@uc": uc},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count_unfetched_urls failed: {}", exc)
            return 0

    def get_affected_control_ids_for_unfetched(self) -> list[str]:
        """Control IDs linked to URLs missing content."""
        cu = COLLECTIONS["control_urls"]
        u = COLLECTIONS["urls"]
        uc = COLLECTIONS["url_content"]
        try:
            cursor = self.db.aql.execute(
                """LET fetched = (FOR d IN @@uc FILTER d.status_code != null RETURN d.url_id)
                   LET unfetched = (FOR d IN @@u FILTER d.url_id NOT IN fetched RETURN d.url_id)
                   FOR c IN @@cu
                       FILTER c.url_id IN unfetched
                       COLLECT cid = c.control_id
                       RETURN cid""",
                bind_vars={"@cu": cu, "@u": u, "@uc": uc},
            )
            return sorted(list(cursor))
        except Exception as exc:
            logger.error("get_affected_control_ids_for_unfetched failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # Readers — regenerate_failed
    # ------------------------------------------------------------------

    def get_anchors_missing_parent_with_controls(self) -> list[dict]:
        """Anchors with missing parent_context, joined with control info."""
        ka = COLLECTIONS["knowledge_anchors"]
        c = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                """FOR a IN @@ka
                    FILTER a.parent_context == null OR LENGTH(a.parent_context || '') < 10
                    LET ctrl = FIRST(FOR d IN @@c FILTER d.control_id == a.control_id RETURN d)
                    RETURN {
                        control_id: a.control_id,
                        name: ctrl.name,
                        framework: ctrl.source_framework
                    }""",
                bind_vars={"@ka": ka, "@c": c},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_anchors_missing_parent_with_controls failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # Readers — backfill_parent_context
    # ------------------------------------------------------------------

    def get_anchors_needing_context(self) -> list[dict]:
        """Anchors with missing parent_context, joined with control metadata."""
        ka = COLLECTIONS["knowledge_anchors"]
        c = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                """FOR a IN @@ka
                    FILTER a.parent_context == null OR LENGTH(a.parent_context || '') < 10
                    LET ctrl = FIRST(FOR d IN @@c FILTER d.control_id == a.control_id RETURN d)
                    RETURN {
                        anchor_id: a.anchor_id,
                        control_id: a.control_id,
                        parent_id: ctrl.parent_id,
                        source_framework: ctrl.source_framework,
                        control_type: ctrl.control_type
                    }""",
                bind_vars={"@ka": ka, "@c": c},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_anchors_needing_context failed: {}", exc)
            return []

    def get_cm_tactic_mappings(self) -> list[dict]:
        """CM -> parent_id via relationships (for backfill_parent_context)."""
        rel = COLLECTIONS["relationships"]
        c = COLLECTIONS["controls"]
        try:
            cursor = self.db.aql.execute(
                """FOR r IN @@rel
                    FILTER STARTS_WITH(r.source_control_id, 'CM')
                    LET target = FIRST(FOR d IN @@c FILTER d.control_id == r.target_control_id RETURN d)
                    FILTER target != null AND target.parent_id != null
                    COLLECT src = r.source_control_id INTO groups
                    RETURN {cm_id: src, parent_id: groups[0].target.parent_id}""",
                bind_vars={"@rel": rel, "@c": c},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("get_cm_tactic_mappings failed: {}", exc)
            return []

    def count_anchors_remaining_no_context(self) -> int:
        """Count anchors still missing parent_context (for verify after backfill)."""
        return self.count_anchors_no_parent_context()

    # ------------------------------------------------------------------
    # Readers — bootstrap items (sparta_pipeline_validator)
    # ------------------------------------------------------------------

    def sample_url_knowledge(self, min_text_len: int = 50, limit: int = 200) -> list[dict]:
        """Sample knowledge chunks for bootstrap labeling."""
        uk = COLLECTIONS["url_knowledge"]
        u = COLLECTIONS["urls"]
        cu = COLLECTIONS["control_urls"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@uk
                    FILTER LENGTH(d.text || '') >= @ml
                    SORT RAND()
                    LIMIT @lim
                    LET url_doc = FIRST(FOR u IN @@u FILTER u.url_id == d.url_id RETURN u)
                    LET ctrl = FIRST(FOR c IN @@cu FILTER c.url_id == d.url_id RETURN c.control_id)
                    RETURN {
                        url_id: d.url_id, excerpt_index: d.excerpt_index,
                        text: d.text, topic: d.topic, excerpt_type: d.excerpt_type,
                        url: url_doc.url, control_id: ctrl
                    }""",
                bind_vars={"@uk": uk, "@u": u, "@cu": cu, "ml": min_text_len, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("sample_url_knowledge failed: {}", exc)
            return []

    def sample_qra_gen_log(self, limit: int = 200) -> list[dict]:
        """Sample QRA generation log entries for prompt_completeness bootstrap."""
        ql = COLLECTIONS["qra_gen_log"]
        c = COLLECTIONS["controls"]
        ka = COLLECTIONS["knowledge_anchors"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@ql
                    FILTER d.ok == true
                    SORT RAND()
                    LIMIT @lim
                    LET ctrl = FIRST(FOR c IN @@c FILTER c.control_id == d.control_id RETURN c)
                    LET anchor = FIRST(FOR a IN @@ka FILTER a.control_id == d.control_id RETURN a)
                    RETURN {
                        request_payload: d.request_payload,
                        control_id: d.control_id,
                        name: ctrl.name,
                        source_framework: ctrl.source_framework,
                        description: ctrl.description,
                        parent_context: anchor.parent_context,
                        child_context: anchor.child_context
                    }""",
                bind_vars={"@ql": ql, "@c": c, "@ka": ka, "lim": limit},
            )
            return list(cursor)
        except Exception as exc:
            logger.error("sample_qra_gen_log failed: {}", exc)
            return []

    def sample_url_content_with_knowledge(self, limit: int = 200) -> list[dict]:
        """Sample url_content with knowledge for reference_coverage bootstrap."""
        uc = COLLECTIONS["url_content"]
        u = COLLECTIONS["urls"]
        el = COLLECTIONS["url_extraction_log"]
        uk = COLLECTIONS["url_knowledge"]
        cu = COLLECTIONS["control_urls"]
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @@uc
                    FILTER d.status_code >= 200 AND d.status_code <= 299
                    SORT RAND()
                    LIMIT @lim
                    LET url_doc = FIRST(FOR u IN @@u FILTER u.url_id == d.url_id RETURN u)
                    LET elog = FIRST(FOR e IN @@el FILTER e.url_id == d.url_id RETURN e)
                    LET chunk = FIRST(FOR k IN @@uk FILTER k.url_id == d.url_id AND k.excerpt_index == 0 RETURN k)
                    LET ctrl = FIRST(FOR c IN @@cu FILTER c.url_id == d.url_id RETURN c.control_id)
                    RETURN {
                        url: url_doc.url, file_path: d.file_path,
                        source_quality: elog.source_quality,
                        text: chunk.text, control_id: ctrl
                    }""",
                bind_vars={
                    "@uc": uc, "@u": u, "@el": el, "@uk": uk, "@cu": cu, "lim": limit,
                },
            )
            return list(cursor)
        except Exception as exc:
            logger.error("sample_url_content_with_knowledge failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # Readers — brandon_reviews (export_brandon_reviews)
    # ------------------------------------------------------------------

    def get_brandon_review_ids(self) -> set:
        """Get all qra_ids that have Brandon reviews."""
        coll = COLLECTIONS["brandon_reviews"]
        try:
            cursor = self.db.aql.execute(
                "FOR d IN @@coll RETURN d.qra_id",
                bind_vars={"@coll": coll},
            )
            return set(cursor)
        except Exception as exc:
            logger.error("get_brandon_review_ids failed: {}", exc)
            return set()

    def get_all_brandon_reviews(self) -> list[dict]:
        """Get all Brandon reviews."""
        coll = COLLECTIONS["brandon_reviews"]
        try:
            cursor = self.db.aql.execute(
                "FOR d IN @@coll SORT d.grade, d.category RETURN d",
                bind_vars={"@coll": coll},
            )
            return [self._strip_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("get_all_brandon_reviews failed: {}", exc)
            return []
