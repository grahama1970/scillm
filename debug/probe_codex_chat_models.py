#!/usr/bin/env python3
from __future__ import annotations
import os, sys, json, time
import urllib.request as rq

BASE = os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089").rstrip("/")
TIMEOUT = float(os.getenv("CODEX_PROBE_TIMEOUT_S", "12"))
TEMP = float(os.getenv("CODEX_PROBE_TEMPERATURE", "1"))

def get(url: str):
    with rq.urlopen(rq.Request(url=url), timeout=TIMEOUT) as resp:
        return int(getattr(resp, 'status', 0) or 0), json.loads(resp.read().decode('utf-8','ignore'))

def post(url: str, payload: dict):
    data = json.dumps(payload).encode('utf-8')
    with rq.urlopen(rq.Request(url=url, data=data, headers={"Content-Type":"application/json"}, method="POST"), timeout=TIMEOUT) as resp:
        return int(getattr(resp, 'status', 0) or 0), json.loads(resp.read().decode('utf-8','ignore'))

def main() -> int:
    print(f"[probe] base={BASE}")
    # health
    try:
        s, h = get(BASE + "/healthz")
        print(f"[probe] /healthz status={s} body={h}")
    except Exception as e:
        print(f"[probe] /healthz error: {e}")
    # models
    try:
        s, m = get(BASE + "/v1/models")
        data = m.get('data') if isinstance(m, dict) else []
        print(f"[probe] models status={s} count={len(data)}")
    except Exception as e:
        print(f"[probe] /v1/models error: {e}")
        return 2
    good = []
    for i, entry in enumerate(data or []):
        mid = str(entry.get('id') if isinstance(entry, dict) else entry)
        if not mid:
            continue
        payload = {
            "model": mid,
            "messages": [
                {"role": "system", "content": "Return strict JSON: {ok:true}"},
                {"role": "user", "content": "ping"},
            ],
            "response_format": {"type": "json_object"},
            "temperature": TEMP,
            "max_tokens": 8,
        }
        try:
            t0 = time.perf_counter()
            s, body = post(BASE + "/v1/chat/completions", payload)
            dt = (time.perf_counter() - t0)
            ok = (s == 200)
            print(f"[probe] {i+1:03d} model={mid} status={s} elapsed={dt:.2f}s ok={ok}")
            if ok:
                good.append(mid)
                # stop at first success unless CODEX_PROBE_ALL=1
                if os.getenv("CODEX_PROBE_ALL", "") != "1":
                    break
        except Exception as e:
            print(f"[probe] {i+1:03d} model={mid} error: {e}")
            continue
    if not good:
        print("[probe] no chat-capable models returned 200; check gateway routing/capabilities.chat")
        return 1
    print(f"[probe] FIRST_OK={good[0]}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

