#!/usr/bin/env python3
from __future__ import annotations

"""
SciLLM Stability Check

Runs a fast, repeatable set of live checks across core components:
- Mini‑Agent (/ready)
- Codex‑Agent (/healthz + one JSON chat)
- Chutes (OpenAI‑compatible JSON mode + pacing interval if enabled)
- Certainly/Lean4 (2 trivial theorems proved)
- CodeWorld bridge (variant-only request; optional MCTS)

Behavior
- Honors environment variables for bases and keys. Does not start containers.
- Exits 0 when all requested checks pass; prints a JSON summary to stdout.
- Use flags to skip components if not applicable.

Examples
  python scripts/scillm_stability_check.py --all
  CHUTES_API_BASE=http://127.0.0.1:18089/v1 CHUTES_API_KEY=sk \
    python scripts/scillm_stability_check.py --chutes --pacing
  python scripts/scillm_stability_check.py --all --autostart --json-out .artifacts/stability.json
"""

import argparse
import subprocess
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import httpx


def _get(env: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(env)
    return v if v else default


def _http_json(method: str, url: str, *, headers: Optional[dict] = None, json_body: Optional[dict] = None, timeout: float = 10.0) -> tuple[int, Any, float]:
    t0 = time.perf_counter()
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method.upper(), url, headers=headers, json=json_body)
        latency = (time.perf_counter() - t0) * 1000.0
        body: Any
        try:
            body = r.json()
        except Exception:
            body = r.text
        return (r.status_code, body, latency)


def check_mini_agent() -> Dict[str, Any]:
    base = _get("MINI_AGENT_API_BASE", "http://127.0.0.1:8788").rstrip("/")
    st, body, lat = _http_json("GET", f"{base}/ready", timeout=6.0)
    ok = (st == 200) and (isinstance(body, dict) and body.get("ok") is True)
    return {"component": "mini_agent", "ok": ok, "status": st, "latency_ms": round(lat, 2), "base": base, "body": body if not ok else {"ok": True}}


def check_codex_agent() -> Dict[str, Any]:
    base = _get("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089").rstrip("/")
    st_h, body_h, _ = _http_json("GET", f"{base}/healthz", timeout=6.0)
    ok_health = (st_h == 200) and (isinstance(body_h, dict) and body_h.get("ok") is True)
    # Minimal JSON-mode chat (OpenAI-compatible)
    payload = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "Return only {\"ok\":true} as JSON."}],
        "response_format": {"type": "json_object"},
    }
    st_c, body_c, lat_c = _http_json("POST", f"{base}/v1/chat/completions", headers={"authorization": "Bearer dummy", "content-type": "application/json"}, json_body=payload, timeout=15.0)
    content = None
    try:
        content = body_c.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(body_c, dict) else None
    except Exception:
        content = None
    ok_chat = (st_c == 200) and bool(content)
    return {"component": "codex_agent", "ok": bool(ok_health and ok_chat), "health": {"status": st_h}, "chat": {"status": st_c, "latency_ms": round(lat_c, 2), "content": content}, "base": base}


def _start_chutes_mock_if_needed() -> Optional[str]:
    """Start a local mock if CHUTES env is absent. Returns base URL if started."""
    if _get("CHUTES_API_BASE") and _get("CHUTES_API_KEY"):
        return None
    # Spawn mock on 127.0.0.1:18093
    base = "http://127.0.0.1:18093/v1"
    try:
        # Quick probe
        st, _, _ = _http_json("GET", f"{base}/models", timeout=1.5)
        if st == 200:
            return base
    except Exception:
        pass
    # Launch mock server in background
    cmd = [sys.executable, "scripts/chutes_mock_server.py"]
    env = os.environ.copy()
    env.setdefault("CHUTES_MOCK_PORT", "18093")
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait briefly
    for _ in range(10):
        try:
            st, _, _ = _http_json("GET", f"{base}/models", timeout=0.5)
            if st == 200:
                return base
        except Exception:
            time.sleep(0.2)
    return None


def check_chutes(pacing: bool = False, autostart: bool = False) -> Dict[str, Any]:
    base = _get("CHUTES_API_BASE")
    key = _get("CHUTES_API_KEY")
    used_mock = False
    if (not base or not key) and autostart:
        mock_base = _start_chutes_mock_if_needed()
        if mock_base:
            base = mock_base
            key = "sk-mock"
            used_mock = True
    if not base or not key:
        return {"component": "chutes", "ok": False, "error": "CHUTES_API_BASE or CHUTES_API_KEY not set"}
    base = base.rstrip("/")
    # Basic JSON-mode call via OpenAI-compatible surface
    model = _get("CHUTES_MODEL") or _get("CHUTES_TEXT_MODEL", "stub-model")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Return only {\"ok\":true} as JSON."}],
        "response_format": {"type": "json_object"},
    }
    hdr = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    st, body, lat = _http_json("POST", f"{base}/chat/completions", headers=hdr, json_body=payload, timeout=15.0)
    if st == 401:
        # Retry with x-api-key
        hdr = {"x-api-key": key, "content-type": "application/json"}
        st, body, lat = _http_json("POST", f"{base}/chat/completions", headers=hdr, json_body=payload, timeout=15.0)
    if st == 401:
        # Retry with raw Authorization (no Bearer)
        hdr = {"authorization": key, "content-type": "application/json"}
        st, body, lat = _http_json("POST", f"{base}/chat/completions", headers=hdr, json_body=payload, timeout=15.0)
    # If model not found and prefixed with openai/, retry with prefix stripped
    if st == 404 and isinstance(body, dict) and str(body.get("detail", "")).startswith("model not found") and model and model.startswith("openai/"):
        suffix = model.split("/", 1)[-1]
        payload["model"] = suffix
        # Prefer raw Authorization for this path
        hdr2 = {"authorization": key, "content-type": "application/json"}
        st, body, lat = _http_json("POST", f"{base}/chat/completions", headers=hdr2, json_body=payload, timeout=15.0)
    content = None
    try:
        content = body.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(body, dict) else None
    except Exception:
        content = None
    ok = (st == 200) and content is not None

    pacing_meta: Dict[str, Any] = {}
    if pacing and ok:
        # Measure spacing between 2 calls; expect >= ~1.0s if SCILLM_QPS_TARGET<=1 is set
        qps = float(_get("SCILLM_QPS_TARGET", "1.0") or 1.0)
        os.environ.setdefault("SCILLM_AUTOSCALE", "1")
        t0 = time.monotonic()
        _http_json("POST", f"{base}/chat/completions", headers=hdr, json_body=payload, timeout=15.0)
        t1 = time.monotonic()
        _http_json("POST", f"{base}/chat/completions", headers=hdr, json_body=payload, timeout=15.0)
        t2 = time.monotonic()
        pacing_meta = {"interval_s": round(t2 - t1, 2), "qps_target": qps}
    result = {"component": "chutes", "ok": ok, "status": st, "latency_ms": round(lat, 2), "content": content, "base": base, "pacing": pacing_meta}
    if used_mock:
        result["note"] = "using mock"
    return result


def check_certainly() -> Dict[str, Any]:
    base = _get("CERTAINLY_BRIDGE_BASE", _get("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8791")).rstrip("/")
    st_h, body_h, _ = _http_json("GET", f"{base}/healthz", timeout=8.0)
    ok_health = (st_h == 200) and (isinstance(body_h, dict) and body_h.get("ok") is True)
    items = [
        {"id": "r1", "requirement_text": "theorem T1 : True := True.intro"},
        {"id": "r2", "requirement_text": "theorem T2 : 1=1 := rfl"},
    ]
    payload = {"messages": [{"role": "system", "content": "Certainly/Lean4"}], "lean4_requirements": items}
    st_p, body_p, lat_p = _http_json("POST", f"{base}/bridge/complete", headers={"content-type": "application/json"}, json_body=payload, timeout=30.0)
    proved = -1
    try:
        proved = int(((body_p or {}).get("summary") or {}).get("proved", -1)) if isinstance(body_p, dict) else -1
    except Exception:
        proved = -1
    ok_proof = (st_p == 200) and (proved >= 2)
    return {"component": "certainly", "ok": bool(ok_health and ok_proof), "health_status": st_h, "prove_status": st_p, "latency_ms": round(lat_p, 2), "proved": proved, "base": base}


def _start_codeworld_if_needed() -> Optional[str]:
    base = "http://127.0.0.1:8887"
    try:
        st, _, _ = _http_json("GET", f"{base}/healthz", timeout=1.5)
        if st == 200:
            return base
    except Exception:
        pass
    # Fall back to running a local container without redis conflict
    try:
        subprocess.run(["docker", "rm", "-f", "scillm-local-codeworld-bridge"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run = [
            "docker", "run", "-d", "--name", "scillm-local-codeworld-bridge",
            "-p", "8887:8887",
            "-e", "CODEWORLD_BASE=http://0.0.0.0:8887",
            "-e", "CODEWORLD_SCORING_NONET=1",
            "-e", "CODEWORLD_STRATEGY_NONET=1",
            "-e", "CODEWORLD_REDIS_URL=redis://host.docker.internal:6379",
            "scillm-bridges-codeworld-bridge",
            "sh", "-lc", "PYTHONPATH=src uvicorn codeworld.bridge.server:app --host 0.0.0.0 --port 8887",
        ]
        subprocess.run(run, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait briefly
        for _ in range(20):
            try:
                st, _, _ = _http_json("GET", f"{base}/healthz", timeout=0.5)
                if st == 200:
                    return base
            except Exception:
                time.sleep(0.25)
    except Exception:
        pass
    return None


def check_codeworld(autostart: bool = False) -> Dict[str, Any]:
    base = _get("CODEWORLD_BASE", "http://127.0.0.1:8888").rstrip("/")
    try:
        st_h, _, _ = _http_json("GET", f"{base}/healthz", timeout=3.0)
    except Exception:
        if autostart:
            started = _start_codeworld_if_needed()
            if started:
                base = started
                st_h, _, _ = _http_json("GET", f"{base}/healthz", timeout=6.0)
            else:
                return {"component": "codeworld", "ok": False, "error": "failed to autostart codeworld"}
        else:
            raise
    items = [{"task": "add", "context": {"code_variants": {"v1": "def add(a,b):\n return a+b", "v2": "def add(a,b):\n return (a+b)"}}}]
    req = {"messages": [{"role": "user", "content": "Pick best variant."}], "provider": {"name": "codeworld", "args": {"strategy": "mcts", "strategy_config": {"name": "mcts", "rollouts": 2, "depth": 1}}}, "items": items}
    st_r, body_r, lat_r = _http_json("POST", f"{base}/bridge/complete", headers={"content-type": "application/json"}, json_body=req, timeout=30.0)
    best = None
    try:
        m = ((body_r or {}).get("run_manifest") or {}) if isinstance(body_r, dict) else {}
        best = ((m.get("mcts_stats") or {}).get("best_variant")) or (m.get("best_variant"))
    except Exception:
        best = None
    ok = (st_h == 200) and (st_r == 200) and bool(best)
    return {"component": "codeworld", "ok": ok, "health_status": st_h, "run_status": st_r, "latency_ms": round(lat_r, 2), "best_variant": best, "base": base}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="run all checks")
    ap.add_argument("--mini", action="store_true", help="check mini-agent")
    ap.add_argument("--codex", action="store_true", help="check codex-agent")
    ap.add_argument("--chutes", action="store_true", help="check chutes")
    ap.add_argument("--certainly", action="store_true", help="check Certainly/Lean4")
    ap.add_argument("--codeworld", action="store_true", help="check CodeWorld bridge")
    ap.add_argument("--pacing", action="store_true", help="measure pacing interval for chutes")
    ap.add_argument("--autostart", action="store_true", help="attempt to autostart local mocks/bridges when missing")
    ap.add_argument("--json-out", type=str, default=None, help="write JSON summary to this path as well as stdout")
    ap.add_argument("--chute-lifecycle", action="store_true", help="start→infer→delete a chute and report timing")
    ap.add_argument("--chute-name", type=str, default=os.getenv("CHUTES_CHUTE_NAME"))
    ap.add_argument("--chute-model", type=str, default=os.getenv("CHUTES_MODEL"))
    args = ap.parse_args()

    if args.all:
        args.mini = args.codex = args.chutes = args.certainly = args.codeworld = True

    results: List[Dict[str, Any]] = []
    if args.mini:
        results.append(check_mini_agent())
    if args.codex:
        results.append(check_codex_agent())
    if args.chutes:
        # Pre-autostart mock if requested and env not set
        if args.autostart and (not _get("CHUTES_API_BASE") or not _get("CHUTES_API_KEY")):
            base = _start_chutes_mock_if_needed()
            if base:
                os.environ.setdefault("CHUTES_API_BASE", base)
                os.environ.setdefault("CHUTES_API_KEY", "sk-mock")
        results.append(check_chutes(pacing=args.pacing, autostart=args.autostart))
    if args.certainly:
        results.append(check_certainly())
    if args.codeworld:
        results.append(check_codeworld(autostart=args.autostart))

    if args.chute_lifecycle:
        # Lazy import to avoid mandatory deps
        try:
            from scillm.extras.chutes import ensure as _ensure_chute, infer as _infer_chute, close as _close_chute
            name = args.chute_name or "scillm_ephemeral_test"
            model = args.chute_model or "deepseek-ai/DeepSeek-R1"
            t0 = time.perf_counter()
            ch = _ensure_chute(name)
            t1 = time.perf_counter()
            out = _infer_chute(ch, model=model, messages=[{"role": "user", "content": "Return only {\\\"ok\\\":true} as JSON."}], response_format={"type": "json_object"})
            t2 = time.perf_counter()
            _close_chute(name)
            t3 = time.perf_counter()
            content = None
            try:
                content = out.get("choices", [{}])[0].get("message", {}).get("content")
            except Exception:
                content = None
            ok = bool(content)
            results.append({
                "component": "chute_lifecycle",
                "ok": ok,
                "startup_sec": round(t1 - t0, 2),
                "infer_ms": round((t2 - t1) * 1000.0, 2),
                "cleanup_ms": round((t3 - t2) * 1000.0, 2),
                "model": model,
                "content": content,
            })
        except Exception as e:
            results.append({"component": "chute_lifecycle", "ok": False, "error": str(e)})

    overall = all(r.get("ok") is True for r in results) if results else False
    summary = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "overall_ok": overall, "results": results}
    out = json.dumps(summary, ensure_ascii=False)
    print(out)
    if args.json_out:
        try:
            os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as f:
                f.write(out)
        except Exception:
            pass
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
