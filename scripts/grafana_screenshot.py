#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

try:
    from pyppeteer import launch  # type: ignore
except Exception:
    print("pyppeteer not installed. Install via: uv run python -m pip install pyppeteer", flush=True)
    raise


GRAFANA_URL = os.getenv("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
ENV_LABEL = os.getenv("METRICS_ENV", "dev")
WINDOW = os.getenv("GRAFANA_AUDIT_WINDOW", "15m")
OUTDIR = Path("artifacts")


def find_dashboards() -> dict[str, str]:
    titles = [
        "SciLLM Overview",
        "SciLLM Budget Lite (Chutes)",
        "Chutes Budget & 429s",
    ]
    out: dict[str, str] = {}
    with httpx.Client(timeout=10) as cx:
        for t in titles:
            r = cx.get(f"{GRAFANA_URL}/api/search", params={"query": t, "type": "dash-db"})
            if r.status_code != 200:
                continue
            for item in r.json():
                if item.get("title") == t:
                    out[t] = item.get("uid")
                    break
    return out


async def screenshot(url: str, out_path: Path) -> None:
    browser = await launch(headless=True, args=["--no-sandbox", "--disable-gpu"])  # nosec B603
    try:
        page = await browser.newPage()
        await page.setViewport({"width": 1400, "height": 900})
        await page.goto(url, {"waitUntil": "networkidle2", "timeout": 30000})
        # Give Grafana a moment to render panels
        await asyncio.sleep(1.0)
        await page.screenshot({"path": str(out_path), "fullPage": True})
    finally:
        await browser.close()


async def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    uids = find_dashboards()
    ts = time.strftime("%Y%m%dT%H%M%S")
    tasks = []
    for title, uid in uids.items():
        url = f"{GRAFANA_URL}/d/{uid}?orgId=1&var-env={ENV_LABEL}&var-window={WINDOW}"
        slug = title.lower().replace(" ", "_").replace("(&)", "").replace("(", "").replace(")", "").replace("/", "_")
        out = OUTDIR / f"{slug}_{ts}.png"
        tasks.append(screenshot(url, out))
    if not tasks:
        print("No dashboards found to screenshot.")
        return 1
    await asyncio.gather(*tasks)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

