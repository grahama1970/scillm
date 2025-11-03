#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
import json
import math
from typing import Any, Dict, List

try:
    import httpx
except Exception as e:
    print("ERROR: httpx is required (uv run python -m pip install httpx)", file=sys.stderr)
    sys.exit(2)


GRAFANA_URL = os.getenv("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
DS_UID_DEFAULT = os.getenv("DS_UID", "SCILLM_PROM")
ENV_LABEL = os.getenv("METRICS_ENV", "dev")
WINDOW = os.getenv("GRAFANA_AUDIT_WINDOW", "15m")
TITLES = [
    "SciLLM Overview",
    "SciLLM Budget Lite (Chutes)",
    "Chutes Budget & 429s",
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _search_dashboard(client: httpx.Client, title: str) -> Dict[str, Any] | None:
    r = client.get(f"{GRAFANA_URL}/api/search", params={"query": title, "type": "dash-db"})
    if r.status_code != 200:
        return None
    for item in r.json():
        if item.get("title") == title:
            return item
    return None


def _get_dashboard_json(client: httpx.Client, uid: str) -> Dict[str, Any] | None:
    r = client.get(f"{GRAFANA_URL}/api/dashboards/uid/{uid}")
    if r.status_code != 200:
        return None
    return r.json().get("dashboard")


def _subst_expr(expr: str, env_label: str, window: str) -> str:
    # Replace $env and $window occurrences (Grafana variable style)
    out = expr.replace("$env", env_label).replace("$window", window)
    return out


def _panel_queries(panel: Dict[str, Any], ds_uid: str, env_label: str, window: str) -> Dict[str, Any] | None:
    targets = panel.get("targets", [])
    if not targets:
        return None
    q = []
    for i, t in enumerate(targets):
        expr = t.get("expr")
        if not expr:
            continue
        expr = _subst_expr(expr, env_label, window)
        q.append({
            "expr": expr,
            "refId": chr(ord('A') + i),
            "datasource": {"type": "prometheus", "uid": ds_uid},
            "exemplar": False,
            "interval": "",
        })
    if not q:
        return None
    now = _now_ms()
    frm = now - 15 * 60 * 1000
    payload = {
        "queries": q,
        "from": str(frm),
        "to": str(now),
    }
    return payload


def _ds_uid_for_panel(panel: Dict[str, Any]) -> str:
    ds = panel.get("datasource")
    if isinstance(ds, dict):
        return ds.get("uid") or DS_UID_DEFAULT
    # string variable case like "${DS_PROMETHEUS}"
    return DS_UID_DEFAULT


def audit_dashboard(client: httpx.Client, title: str) -> Dict[str, Any]:
    out = {"title": title, "panels": []}
    item = _search_dashboard(client, title)
    if not item:
        out["error"] = "not_found"
        return out
    uid = item.get("uid")
    dash = _get_dashboard_json(client, uid)
    if not dash:
        out["error"] = "load_failed"
        return out
    for pan in dash.get("panels", []):
        # Rows have no targets
        if pan.get("type") == "row":
            continue
        ds_uid = _ds_uid_for_panel(pan)
        payload = _panel_queries(pan, ds_uid, ENV_LABEL, WINDOW)
        if not payload:
            continue
        # Post to Grafana /api/ds/query (anonymous)
        r = client.post(f"{GRAFANA_URL}/api/ds/query?ds_type=prometheus", json=payload)
        status = r.status_code
        ok = status == 200
        sample = None
        if ok:
            try:
                js = r.json()
                # Count frames or series presence
                results = js.get("results", {})
                frames = sum(len(v.get("frames", [])) for v in results.values())
                sample = {"targets": len(payload["queries"]), "frames": frames}
            except Exception:
                sample = {"targets": len(payload["queries"]), "frames": None}
        out["panels"].append({
            "id": pan.get("id"),
            "title": pan.get("title"),
            "status": status,
            "ok": ok,
            "sample": sample,
        })
    return out


def main() -> int:
    reports: List[Dict[str, Any]] = []
    with httpx.Client(timeout=10) as cx:
        # Health quick check
        try:
            h = cx.get(f"{GRAFANA_URL}/api/health").json()
        except Exception:
            print(json.dumps({"ok": False, "error": "grafana_unreachable", "base": GRAFANA_URL}), flush=True)
            return 2
        for t in TITLES:
            rep = audit_dashboard(cx, t)
            reports.append(rep)
    # Summarize
    passed = all(all(p.get("ok") for p in r.get("panels", [])) for r in reports if not r.get("error"))
    result = {"ok": passed, "env": ENV_LABEL, "window": WINDOW, "base": GRAFANA_URL, "reports": reports}
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

