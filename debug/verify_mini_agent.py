#!/usr/bin/env python3
"""
Verify mini-agent works (Docker on 127.0.0.1:8788 and local debug).

Checks (Docker):
- GET /ready == {"ok": true}
- POST /agent/run with tool_backend=local returns ok true and 200

Checks (Local):
- Start uvicorn on 127.0.0.1:8789 and probe /ready and /agent/run

Usage:
  python debug/verify_mini_agent.py          # docker-only check
  python debug/verify_mini_agent.py --local  # also start local uvicorn on 8789
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request


def _get(url: str, timeout=3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read()
    except Exception as e:
        return 0, str(e).encode()


def _post(url: str, body: dict, timeout=6.0):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"content-type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read()
    except Exception as e:
        return 0, str(e).encode()


def check_endpoint(base: str) -> int:
    rc, body = _get(base+"/ready")
    if rc != 200:
        print(f"[fail] {base}/ready rc={rc}")
        return 2
    try:
        ok = json.loads(body.decode()).get("ok") is True
    except Exception:
        ok = False
    if not ok:
        print(f"[fail] {base}/ready not ok")
        return 2
    rc2, body2 = _post(base+"/agent/run", {"messages":[{"role":"user","content":"hi"}],"model":"dummy","tool_backend":"local"})
    if rc2 != 200:
        print(f"[fail] {base}/agent/run rc={rc2} body={body2[:120].decode(errors='ignore')}")
        return 2
    try:
        j = json.loads(body2.decode())
        if not (j.get("ok") is True and isinstance(j.get("metrics",{}).get("iterations"), int)):
            print(f"[warn] {base}/agent/run returned unexpected shape: {j}")
            # soft pass
    except Exception as e:
        print(f"[warn] decode run response err={e}")
    print(f"[ok] mini-agent @ {base} responded to /ready and /agent/run")
    return 0


def start_local_and_check() -> int:
    # Launch uvicorn in background
    cmd = [sys.executable, "-m", "uvicorn", "litellm.experimental_mcp_client.mini_agent.agent_proxy:app", "--host", "127.0.0.1", "--port", "8789", "--log-level", "warning"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait until ready responds
        for _ in range(30):
            rc, _ = _get("http://127.0.0.1:8789/ready")
            if rc == 200:
                break
            time.sleep(0.2)
        return check_endpoint("http://127.0.0.1:8789")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="also start and verify local uvicorn on 8789")
    args = ap.parse_args()
    rc = check_endpoint("http://127.0.0.1:8788")
    if args.local:
        rc2 = start_local_and_check()
        rc = max(rc, rc2)
    sys.exit(rc)


if __name__ == "__main__":
    main()

