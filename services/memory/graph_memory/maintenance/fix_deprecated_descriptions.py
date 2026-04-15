#!/usr/bin/env python3
"""Backfill descriptions for deprecated controls with empty description fields.

Sources:
- NIST rev4 CSV: provides [Withdrawn: ...] text for deprecated NIST controls
- ATT&CK: generates deprecation notice from name/framework
- All others: generates deprecation notice

Usage:
    python -m graph_memory.maintenance.fix_deprecated_descriptions [--dry-run]
"""
import csv
import sys
from pathlib import Path

from graph_memory.arango_client import get_db


NIST_REV4_CSV = Path(__file__).parents[4] / "sparta" / "data" / "raw" / "nist_rev4_controls.csv"


def load_nist_rev4_descriptions() -> dict[str, str]:
    """Load withdrawn control descriptions from NIST SP 800-53 rev4 CSV."""
    descriptions = {}
    if not NIST_REV4_CSV.exists():
        print(f"  WARN: NIST rev4 CSV not found at {NIST_REV4_CSV}")
        return descriptions

    with open(NIST_REV4_CSV) as f:
        for row in csv.DictReader(f):
            name = row.get("NAME", "").strip()
            desc = row.get("DESCRIPTION", "").strip()
            if name and desc:
                descriptions[name] = desc
    print(f"  Loaded {len(descriptions)} NIST rev4 descriptions")
    return descriptions


def find_empty_descriptions(db) -> list[dict]:
    """Find all controls with empty or null descriptions."""
    query = """
    FOR d IN sparta_controls
        FILTER d.description == null OR d.description == ""
        RETURN {
            _key: d._key,
            control_id: d.control_id,
            name: d.name,
            source_framework: d.source_framework,
            status: d.status,
            control_type: d.control_type
        }
    """
    return list(db.aql.execute(query))


def generate_description(ctrl: dict, nist_rev4: dict) -> str:
    """Generate a description for a control with no description."""
    cid = ctrl["control_id"]
    name = ctrl.get("name", "")
    fw = ctrl.get("source_framework", "")
    ctype = ctrl.get("control_type", "control")
    status = ctrl.get("status", "")

    # Try NIST rev4 CSV first (has specific withdrawal notices)
    if cid in nist_rev4:
        return nist_rev4[cid]

    # Generate deprecation notice
    if status == "deprecated":
        return f"[Deprecated] {name}. This {fw} {ctype} has been withdrawn or deprecated in the current framework revision."

    # Fallback: use the name
    return f"{name} — {fw} {ctype}."


def main():
    dry_run = "--dry-run" in sys.argv

    print("=== Fix Deprecated Control Descriptions ===\n")

    db = get_db()
    nist_rev4 = load_nist_rev4_descriptions()
    empty = find_empty_descriptions(db)

    print(f"  Found {len(empty)} controls with empty descriptions\n")

    if not empty:
        print("Nothing to fix.")
        return 0

    # Group by framework for reporting
    by_fw: dict[str, list] = {}
    for ctrl in empty:
        fw = ctrl.get("source_framework", "unknown")
        by_fw.setdefault(fw, []).append(ctrl)

    for fw, ctrls in sorted(by_fw.items()):
        print(f"  {fw}: {len(ctrls)}")

    # Update
    updated = 0
    coll = db.collection("sparta_controls")
    for ctrl in empty:
        desc = generate_description(ctrl, nist_rev4)
        if dry_run:
            print(f"  [DRY] {ctrl['control_id']}: {desc[:80]}")
        else:
            coll.update({"_key": ctrl["_key"], "description": desc})
            updated += 1

    print(f"\n{'Would update' if dry_run else 'Updated'}: {updated}")

    # Verify
    if not dry_run:
        still_empty = find_empty_descriptions(db)
        print(f"Still empty: {len(still_empty)}")
        if still_empty:
            for ctrl in still_empty[:5]:
                print(f"  {ctrl['control_id']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
