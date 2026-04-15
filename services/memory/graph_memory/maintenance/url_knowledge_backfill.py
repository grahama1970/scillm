"""Backfill control metadata onto sparta_url_knowledge documents.

Denormalizes control_id, tactic, and source_framework from the junction
table (sparta_control_urls -> sparta_controls) onto each url_knowledge doc.
Also resolves parent tactics via both parent_id and sparta_relationships.

Includes orphan junction backfill: creates ``sparta_control_urls`` entries
for url_knowledge docs that have no junction entry but whose URL can be
resolved to a control_id (ATT&CK software, tactics, mitigations,
Spaceshield techniques).

Uses parameterized AQL (@@collection bind vars) throughout.
Idempotent — safe to re-run.
"""
from __future__ import annotations

import re
import time
from typing import Any

from loguru import logger


# Collection names — single source of truth matches sparta_data_bridge.COLLECTIONS
_UK = "sparta_url_knowledge"
_CU = "sparta_control_urls"
_C = "sparta_controls"
_REL = "sparta_relationships"
_U = "sparta_urls"

_TACTIC_TYPES = ["tactic", "attack_tactic", "sparta_tactic"]


def backfill_control_ids(db) -> dict[str, Any]:
    """Denormalize control_id, tactic, source_framework onto url_knowledge.

    Args:
        db: python-arango database handle

    Returns:
        Summary dict with counts of updated/skipped docs and tactic stats.
    """
    # Step 1: url_id -> [control details] in ONE AQL round-trip
    url_controls: dict[int, list[dict]] = {
        r["url_id"]: r["controls"]
        for r in db.aql.execute(
            """FOR cu IN @@cu
                FOR ctrl IN @@c
                    FILTER ctrl.control_id == cu.control_id
                    COLLECT uid = cu.url_id INTO controls = {
                        control_id: ctrl.control_id,
                        name: ctrl.name,
                        source_framework: ctrl.source_framework,
                        control_type: ctrl.control_type,
                        parent_id: ctrl.parent_id
                    }
                    RETURN {url_id: uid, controls: controls}""",
            bind_vars={"@cu": _CU, "@c": _C},
            ttl=600,
        )
    }
    logger.info("url_id->controls mapping: {} url_ids", len(url_controls))

    # Step 2a: parent_id -> tactic name
    tactic_map: dict[str, str] = {
        r["id"]: r["name"]
        for r in db.aql.execute(
            """FOR ctrl IN @@c
                FILTER ctrl.control_type IN @types
                RETURN {id: ctrl.control_id, name: ctrl.name}""",
            bind_vars={"@c": _C, "types": _TACTIC_TYPES},
        )
    }
    logger.info("Tactics from parent_id: {} found", len(tactic_map))

    # Step 2b: technique -> [tactic names] via relationships
    rel_tactic_map = _build_relationship_tactic_map(db)

    # Step 3: batch update
    batch_size = 1000
    updated = 0
    skipped = 0
    total = 0

    cursor = db.aql.execute(
        "FOR doc IN @@uk RETURN {_key: doc._key, url_id: doc.url_id}",
        bind_vars={"@uk": _UK},
        ttl=600, batch_size=batch_size,
    )

    batch: list[dict] = []
    for doc in cursor:
        total += 1
        controls = url_controls.get(doc["url_id"])
        if not controls:
            skipped += 1
            continue

        update = _build_update_doc(doc["_key"], controls, tactic_map, rel_tactic_map)
        batch.append(update)

        if len(batch) >= batch_size:
            db.collection(_UK).update_many(batch)
            updated += len(batch)
            batch = []

    if batch:
        db.collection(_UK).update_many(batch)
        updated += len(batch)

    return {
        "total_docs": total,
        "updated": updated,
        "skipped_no_control_match": skipped,
        "url_ids_with_controls": len(url_controls),
        "tactics_from_parent_id": len(tactic_map),
        "tactics_from_relationships": len(rel_tactic_map),
    }


def get_orphan_url_knowledge(db, limit: int = 50) -> dict[str, Any]:
    """Find url_knowledge docs with no entry in control_urls junction.

    Returns sample orphans with domain breakdown to determine whether
    these are genuinely unlinked scrapes or junction table gaps.
    """
    sample_aql = """
    LET linked = (FOR cu IN @@cu RETURN DISTINCT cu.url_id)
    FOR doc IN @@uk
        FILTER doc.url_id NOT IN linked
        LIMIT @lim
        LET url_info = FIRST(
            FOR u IN @@u FILTER u.url_id == doc.url_id
            RETURN {url: u.url, domain: u.domain}
        )
        RETURN {
            _key: doc._key, url_id: doc.url_id, topic: doc.topic,
            excerpt_index: doc.excerpt_index, url: url_info.url,
            domain: url_info.domain
        }
    """
    orphans = list(db.aql.execute(
        sample_aql,
        bind_vars={"@cu": _CU, "@uk": _UK, "@u": _U, "lim": limit},
        ttl=300,
    ))

    count_aql = """
    LET linked = (FOR cu IN @@cu RETURN DISTINCT cu.url_id)
    FOR doc IN @@uk
        FILTER doc.url_id NOT IN linked
        COLLECT WITH COUNT INTO c RETURN c
    """
    total = db.aql.execute(
        count_aql,
        bind_vars={"@cu": _CU, "@uk": _UK},
        ttl=300,
    ).next()

    domain_aql = """
    LET linked = (FOR cu IN @@cu RETURN DISTINCT cu.url_id)
    FOR doc IN @@uk
        FILTER doc.url_id NOT IN linked
        LET d = FIRST(FOR u IN @@u FILTER u.url_id == doc.url_id RETURN u.domain)
        COLLECT domain = d WITH COUNT INTO cnt
        SORT cnt DESC LIMIT 20
        RETURN {domain: domain, count: cnt}
    """
    domains = list(db.aql.execute(
        domain_aql,
        bind_vars={"@cu": _CU, "@uk": _UK, "@u": _U},
        ttl=300,
    ))

    return {
        "total_orphan_docs": total,
        "domain_breakdown": domains,
        "sample_orphans": orphans,
    }


def backfill_orphan_junctions(db) -> dict[str, Any]:
    """Create junction entries for orphaned url_knowledge docs.

    Finds url_ids in ``sparta_url_knowledge`` that have no entry in
    ``sparta_control_urls``, resolves each URL to a control_id where
    possible, inserts the junction row, then re-runs ``backfill_control_ids``
    to populate control metadata on the newly linked docs.

    Resolution strategies (in order):
      1. ATT&CK software URLs -> S-code control_id  (e.g. S0111)
      2. ATT&CK tactic URLs   -> ST-code via name match
      3. ATT&CK mitigation URLs -> M-code control_id (e.g. M1031)
      4. ATT&CK technique URLs -> T-code control_id  (e.g. T1629.001)
      5. Spaceshield technique URLs -> T-code (slash->dot, e.g. T1195.001)

    Args:
        db: python-arango database handle

    Returns:
        Summary dict with counts per strategy and backfill_control_ids result.
    """
    ts = int(time.time())

    # ── Step 1: Collect orphan url_ids with their URLs ───────────
    orphan_aql = """
    LET linked = (FOR cu IN @@cu RETURN DISTINCT cu.url_id)
    FOR doc IN @@uk
        FILTER doc.url_id NOT IN linked
        COLLECT uid = doc.url_id
        LET url_doc = FIRST(FOR u IN @@u FILTER u.url_id == uid RETURN u)
        RETURN {url_id: uid, url: url_doc.url}
    """
    orphans = list(db.aql.execute(
        orphan_aql,
        bind_vars={"@cu": _CU, "@uk": _UK, "@u": _U},
        ttl=600,
    ))
    logger.info("Orphan url_ids found: {}", len(orphans))

    if not orphans:
        return {
            "orphans_found": 0,
            "junctions_created": 0,
            "backfill_result": None,
        }

    # ── Step 2: Classify orphans by URL pattern ──────────────────
    software: list[tuple[int, str]] = []     # (url_id, S-code)
    tactics: list[tuple[int, str]] = []      # (url_id, TA-code)
    mitigations: list[tuple[int, str]] = []  # (url_id, M-code)
    techniques: list[tuple[int, str]] = []   # (url_id, T-code)
    spaceshield: list[tuple[int, str]] = []  # (url_id, T-code.sub)
    unresolvable: list[int] = []

    for o in orphans:
        url = o.get("url") or ""
        url_id = o["url_id"]

        m = re.match(r"https://attack\.mitre\.org/software/(S\d+)", url)
        if m:
            software.append((url_id, m.group(1)))
            continue

        m = re.match(r"https://attack\.mitre\.org/tactics/(TA\d+)", url)
        if m:
            tactics.append((url_id, m.group(1)))
            continue

        m = re.match(r"https://attack\.mitre\.org/mitigations/(M\d+)", url)
        if m:
            mitigations.append((url_id, m.group(1)))
            continue

        m = re.match(
            r"https://attack\.mitre\.org/techniques/(T\d+(?:/\d+)?)", url,
        )
        if m:
            ctrl_id = m.group(1).replace("/", ".")
            techniques.append((url_id, ctrl_id))
            continue

        m = re.match(
            r"https://spaceshield\.esa\.int/techniques/(T\d+(?:/\d+)?)", url,
        )
        if m:
            ctrl_id = m.group(1).replace("/", ".")
            spaceshield.append((url_id, ctrl_id))
            continue

        unresolvable.append(url_id)

    logger.info(
        "Classified: software={} tactics={} mitigations={} techniques={} "
        "spaceshield={} unresolvable={}",
        len(software), len(tactics), len(mitigations),
        len(techniques), len(spaceshield), len(unresolvable),
    )

    # ── Step 3: Validate control_ids exist in sparta_controls ────
    # Collect all candidate control_ids for batch validation.
    direct_ids = (
        [sid for _, sid in software]
        + [mid for _, mid in mitigations]
        + [tid for _, tid in techniques]
        + [tid for _, tid in spaceshield]
    )

    valid_controls: set[str] = set()
    if direct_ids:
        valid_controls = set(db.aql.execute(
            "FOR c IN @@c FILTER c.control_id IN @ids RETURN c.control_id",
            bind_vars={"@c": _C, "ids": list(set(direct_ids))},
        ))

    # ── Step 4: Resolve ATT&CK tactics via name matching ─────────
    # ATT&CK TA-codes don't exist in sparta_controls (which uses ST-codes).
    # Build TA-code -> tactic name mapping, then match by name to ST-codes.
    tactic_junctions: list[tuple[int, str]] = []
    if tactics:
        ta_name_map = _attack_tactic_names()
        # Get tactic name -> control_id from sparta_controls
        name_to_ctrl: dict[str, str] = {}
        for r in db.aql.execute(
            "FOR c IN @@c FILTER c.control_type IN @types "
            "RETURN {id: c.control_id, name: c.name}",
            bind_vars={"@c": _C, "types": _TACTIC_TYPES},
        ):
            name_to_ctrl[r["name"].lower()] = r["id"]

        for url_id, ta_code in tactics:
            tactic_name = ta_name_map.get(ta_code, "").lower()
            ctrl_id = name_to_ctrl.get(tactic_name)
            if ctrl_id:
                tactic_junctions.append((url_id, ctrl_id))
            else:
                logger.debug(
                    "Tactic {} ({}) has no matching control",
                    ta_code, tactic_name,
                )

    # ── Step 5: Build junction docs ──────────────────────────────
    junction_docs: list[dict] = []

    for url_id, ctrl_id in software:
        if ctrl_id in valid_controls:
            junction_docs.append(_junction_doc(ctrl_id, url_id, ts))

    for url_id, ctrl_id in mitigations:
        if ctrl_id in valid_controls:
            junction_docs.append(_junction_doc(ctrl_id, url_id, ts))

    for url_id, ctrl_id in techniques:
        if ctrl_id in valid_controls:
            junction_docs.append(_junction_doc(ctrl_id, url_id, ts))

    for url_id, ctrl_id in spaceshield:
        if ctrl_id in valid_controls:
            junction_docs.append(_junction_doc(ctrl_id, url_id, ts))

    for url_id, ctrl_id in tactic_junctions:
        junction_docs.append(_junction_doc(ctrl_id, url_id, ts))

    logger.info("Junction docs to insert: {}", len(junction_docs))

    # ── Step 6: Insert junction entries (idempotent via overwrite) ─
    created = 0
    if junction_docs:
        col = db.collection(_CU)
        result = col.insert_many(junction_docs, overwrite=True)
        created = sum(1 for r in result if not isinstance(r, Exception))
        logger.info("Junction entries inserted: {}", created)

    # ── Step 7: Re-run control_id backfill for newly linked docs ─
    backfill_result = None
    if created > 0:
        backfill_result = backfill_control_ids(db)
        logger.info("Post-junction backfill: {}", backfill_result)

    return {
        "orphans_found": len(orphans),
        "classified": {
            "software": len(software),
            "tactics": len(tactics),
            "mitigations": len(mitigations),
            "techniques": len(techniques),
            "spaceshield": len(spaceshield),
            "unresolvable": len(unresolvable),
        },
        "valid_direct_controls": len(valid_controls),
        "tactic_name_matches": len(tactic_junctions),
        "junctions_created": created,
        "backfill_result": backfill_result,
    }


# ── helpers ────────────────────────────────────────────────────


def _junction_doc(control_id: str, url_id: int, ts: int) -> dict:
    """Build a single sparta_control_urls junction document."""
    return {
        "_key": f"cu__{control_id}__{url_id}",
        "control_id": control_id,
        "url_id": url_id,
        "updated_at": ts,
    }


def _attack_tactic_names() -> dict[str, str]:
    """Well-known ATT&CK tactic TA-code to name mapping.

    Covers Enterprise, Mobile, and ICS matrices.
    """
    return {
        "TA0001": "Initial Access",
        "TA0002": "Execution",
        "TA0003": "Persistence",
        "TA0004": "Privilege Escalation",
        "TA0005": "Defense Evasion",
        "TA0006": "Credential Access",
        "TA0007": "Discovery",
        "TA0008": "Lateral Movement",
        "TA0009": "Collection",
        "TA0010": "Exfiltration",
        "TA0011": "Command and Control",
        "TA0040": "Impact",
        "TA0042": "Resource Development",
        "TA0043": "Reconnaissance",
        # Mobile
        "TA0027": "Initial Access",
        "TA0031": "Persistence",
        "TA0032": "Privilege Escalation",
        # ICS
        "TA0105": "Initial Access",
        "TA0109": "Lateral Movement",
        "TA0110": "Execution",
    }


def _build_relationship_tactic_map(db) -> dict[str, list[str]]:
    """Build technique -> [tactic names] via sparta_relationships."""
    existing = {
        c["name"] for c in db.collections() if not c["name"].startswith("_")
    }
    if _REL not in existing:
        logger.info("sparta_relationships not found, skipping relationship tactic lookup")
        return {}

    rel_tactic_map: dict[str, list[str]] = {}
    try:
        for r in db.aql.execute(
            """FOR r IN @@rel
                FOR src IN @@c
                    FILTER src.control_id == r.source_control_id
                    FILTER src.control_type IN @types
                    RETURN {technique: r.target_control_id, tactic_name: src.name}""",
            bind_vars={"@rel": _REL, "@c": _C, "types": _TACTIC_TYPES},
            ttl=300,
        ):
            tech = r["technique"]
            tname = r["tactic_name"]
            if tech not in rel_tactic_map:
                rel_tactic_map[tech] = []
            if tname not in rel_tactic_map[tech]:
                rel_tactic_map[tech].append(tname)
        logger.info("Tactics from relationships: {} techniques mapped", len(rel_tactic_map))
    except Exception as exc:
        logger.warning("Relationship tactic lookup failed (non-fatal): {}", exc)

    return rel_tactic_map


def _build_update_doc(
    key: str,
    controls: list[dict],
    tactic_map: dict[str, str],
    rel_tactic_map: dict[str, list[str]],
) -> dict[str, Any]:
    """Build the update document for a single url_knowledge doc."""
    control_ids = sorted(set(c["control_id"] for c in controls))
    frameworks = sorted(set(
        c["source_framework"] for c in controls if c["source_framework"]
    ))
    parent_ids = sorted(set(
        c["parent_id"] for c in controls if c.get("parent_id")
    ))

    # Resolve tactics from BOTH parent_id and relationships
    tactics_set: set[str] = set()
    for pid in parent_ids:
        if pid in tactic_map:
            tactics_set.add(tactic_map[pid])
    for cid in control_ids:
        if cid in rel_tactic_map:
            tactics_set.update(rel_tactic_map[cid])
    tactics = sorted(tactics_set)

    return {
        "_key": key,
        "control_id": control_ids[0],
        "control_ids": control_ids,
        "source_framework": frameworks[0] if frameworks else None,
        "source_frameworks": frameworks,
        "tactic": tactics[0] if len(tactics) == 1 else None,
        "tactics": tactics,
        "parent_ids": parent_ids,
        "control_count": len(control_ids),
    }
