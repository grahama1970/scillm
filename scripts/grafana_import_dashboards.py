#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import httpx
except Exception:
    httpx = None


def post_dashboard(url: str, token: str, dashboard_path: str, folder_uid: str | None, overwrite: bool = True) -> tuple[int, str]:
    if httpx is None:
        return 2, "httpx not installed"
    with open(dashboard_path, "r", encoding="utf-8") as f:
        dash = json.load(f)
    payload = {"dashboard": dash, "overwrite": bool(overwrite)}
    if folder_uid:
        payload["folderUid"] = folder_uid
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    api = url.rstrip("/") + "/api/dashboards/db"
    with httpx.Client(timeout=15) as cx:
        r = cx.post(api, headers=headers, json=payload)
        return r.status_code, r.text


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Grafana dashboards")
    # Support both standard and SCILLM-specific env names for convenience
    default_url = os.getenv("GRAFANA_URL") or os.getenv("GRAFANA_SCILLM_GRAFANA_URL")
    default_token = os.getenv("GRAFANA_TOKEN") or os.getenv("GRAFANA_SCILLM_SERVICE_TOKEN")

    ap.add_argument("--url", default=default_url, help="Grafana base URL (e.g., https://grafana.local)")
    ap.add_argument("--token", default=default_token, help="Grafana Service Account Token (SAT)")
    ap.add_argument("--folder", default=os.getenv("GRAFANA_FOLDER_UID"), help="Folder UID (optional)")
    ap.add_argument("--dash", action="append", help="Dashboard JSON file (repeatable)")
    args = ap.parse_args()

    if not args.url or not args.token:
        print("ERROR: Set --url/--token or GRAFANA_URL/GRAFANA_TOKEN", file=sys.stderr)
        return 2

    dashboards = args.dash or [
        "dashboards/scillm_overview_grafana.json",
        "dashboards/chutes_grafana_dashboard.json",
    ]

    rc = 0
    for d in dashboards:
        code, body = post_dashboard(args.url, args.token, d, args.folder)
        print(f"POST {d} -> {code}")
        if code >= 300:
            print(body[:500])
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
