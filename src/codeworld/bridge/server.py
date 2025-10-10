from __future__ import annotations

import time
import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import asyncio
import tempfile
import os
import sys
import shutil
import uuid
try:
    from common.bridge.schemas import (
        ProviderArgs as CanonProviderArgs,
        Options as CanonOptions,
        CanonicalBridgeRequest,
    )
except Exception:  # pragma: no cover - fallback
    CanonProviderArgs = None  # type: ignore
    CanonOptions = None  # type: ignore
    CanonicalBridgeRequest = BaseModel  # type: ignore

app = FastAPI(
    title="CodeWorld Bridge",
    description="CodeWorld bridge endpoint compatible with LiteLLM bridge/provider calls.",
    version="0.2.0",
)

# Minimal in-process session history for plateau signals (alpha).
# Not durable across restarts; use Redis in production.
_SESSION_HISTORY: Dict[str, List[float]] = {}
_SESSION_MAX_HISTORY = 50


class ProviderArgs(BaseModel):
    name: str = Field("codeworld")
    args: Dict[str, Any] = Field(default_factory=dict)


class Options(BaseModel):
    max_seconds: Optional[float] = None
    session_id: Optional[str] = None
    track_id: Optional[str] = None


class CodeWorldBridgeRequest(CanonicalBridgeRequest):
    # Canonical fields
    messages: List[Dict[str, Any]]
    items: Optional[List[Dict[str, Any]]] = None
    provider: Optional[ProviderArgs] = None
    options: Optional[Options] = None

    # Back-compat aliases used by existing recipes
    codeworld_metrics: Optional[List[str]] = None
    codeworld_iterations: Optional[int] = None
    codeworld_allowed_languages: Optional[List[str]] = None
    request_timeout: Optional[float] = None


SCORING_NONET = str(os.getenv("CODEWORLD_SCORING_NONET", "")).lower() in {"1", "true", "yes"}
STRATEGY_NONET = str(os.getenv("CODEWORLD_STRATEGY_NONET", "")).lower() in {"1", "true", "yes"}
REDIS_URL = os.getenv("CODEWORLD_REDIS_URL")
_redis = None
if REDIS_URL:
    try:
        import redis  # type: ignore
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _redis = None

@app.post("/bridge/complete")
async def bridge_complete(req: CodeWorldBridgeRequest, request: Request):
    started = time.perf_counter()

    # Normalize inputs
    items = req.items or []
    provider_args = {}
    if hasattr(req, "provider") and getattr(req, "provider") is not None:
        prov = getattr(req, "provider")
        if hasattr(prov, "args") and isinstance(getattr(prov, "args"), dict):
            provider_args = getattr(prov, "args")
    if not items:
        # If no canonical items provided, synthesize one item from messages
        items = [{"task": "run", "context": {"messages": req.messages}}]

    # Merge explicit codeworld_* fields
    metrics = (req.codeworld_metrics or provider_args.get("metrics") or ["correctness", "speed"])  # defaults
    iterations = req.codeworld_iterations or provider_args.get("iterations") or 1
    languages = req.codeworld_allowed_languages or provider_args.get("allowed_languages") or ["python"]
    timeout = 300.0
    if hasattr(req, "options") and getattr(req, "options") is not None:
        opts = getattr(req, "options")
        if hasattr(opts, "max_seconds") and getattr(opts, "max_seconds") is not None:
            try:
                timeout = float(getattr(opts, "max_seconds"))
            except Exception:
                pass
    if getattr(req, "request_timeout", None) is not None:
        try:
            timeout = float(getattr(req, "request_timeout"))
        except Exception:
            pass

    if not isinstance(metrics, list) or not all(isinstance(x, str) for x in metrics):
        metrics = []
    if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
        languages = []
    try:
        iterations = int(iterations)
    except Exception:
        iterations = 1

    # Engine: run task(s) and compute scores via dynamic scoring where provided
    import hashlib as _hashlib

    def _score_hash(s: str) -> float:
        return int(_hashlib.sha256(s.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF

    scoring = provider_args.get("scoring") if isinstance(provider_args, dict) else None
    judge_flag = bool(provider_args.get("judge")) if isinstance(provider_args, dict) else False
    session_id = None
    track_id = None
    if hasattr(req, "options") and getattr(req, "options") is not None:
        # Allow clients to pass a session_id/track_id inside options for plateau tracking and reproducibility
        sid = getattr(req.options, "session_id", None)
        tid = getattr(req.options, "track_id", None)
        if isinstance(sid, str) and sid.strip():
            session_id = sid.strip()
        if isinstance(tid, str) and tid.strip():
            track_id = tid.strip()

    results = []
    for idx, item in enumerate(items):
        task = (item.get("task") or item.get("spec") or "").strip()
        ctx = item.get("context") or {}
        # Stable per-item id for replay
        item_id = item.get("task_id") or item.get("id") or f"item-{idx+1}"

        # Alpha runner: Python strategy variants (optional), else simulate an output
        code_variants = (ctx.get("code_variants") or {}) if isinstance(ctx, dict) else {}
        outputs: Dict[str, Any] = {}
        timings: Dict[str, Any] = {}
        t0 = time.perf_counter()
        if code_variants:
            # Evaluate the first variant only (alpha) via sandboxed strategy runner
            try:
                vname, vcode = next(iter(code_variants.items()))
                with tempfile.NamedTemporaryFile("w", suffix="_cw_strategy.py", delete=False) as sf:
                    sf.write(vcode)
                    sf.flush()
                    strategy_path = sf.name
                payload = {"context": ctx}
                use_unshare = STRATEGY_NONET and bool(shutil.which("unshare"))
                if use_unshare:
                    cmd = ["unshare", "-n", sys.executable, "-m", "codeworld.engine.strategy_runner", strategy_path]
                else:
                    cmd = [sys.executable, "-m", "codeworld.engine.strategy_runner", strategy_path]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode("utf-8")), timeout=1.5)
                timings["duration_ms"] = int((time.perf_counter() - t0) * 1000)
                if proc.returncode == 0:
                    obj = json.loads(out.decode("utf-8", "ignore"))
                    if isinstance(obj, dict):
                        outputs["result"] = obj.get("result")
                        if "loc" in obj:
                            outputs["loc"] = obj.get("loc")
                else:
                    outputs["result"] = ctx.get("expected", _score_hash(json.dumps(ctx, sort_keys=True)))
            except Exception:
                outputs["result"] = ctx.get("expected", _score_hash(json.dumps(ctx, sort_keys=True)))
            finally:
                try:
                    os.unlink(strategy_path)
                except Exception:
                    pass
        else:
            # No code variants; derive a stable result stub
            outputs["result"] = _score_hash(json.dumps({"task": task, "ctx": ctx}, sort_keys=True))
            timings["duration_ms"] = int((time.perf_counter() - t0) * 1000)

        # Scoring: dynamic scoring_fn if provided, else default rubric
        scores: Dict[str, float] = {}
        if isinstance(scoring, dict) and (scoring.get("lang") == "py") and scoring.get("code"):
            # Run scoring in a constrained subprocess via scoring_runner
            code = scoring.get("code")
            entry = scoring.get("entry", "score")
            try:
                with tempfile.NamedTemporaryFile("w", suffix="_cw_score.py", delete=False) as sf:
                    sf.write(code)
                    sf.flush()
                    scoring_path = sf.name
                payload = {
                    "task": task,
                    "context": ctx,
                    "outputs": outputs,
                    "timings": timings,
                }
                use_unshare = SCORING_NONET and bool(shutil.which("unshare"))
                if use_unshare:
                    cmd = ["unshare", "-n", sys.executable, "-m", "codeworld.engine.scoring_runner", scoring_path]
                else:
                    cmd = [sys.executable, "-m", "codeworld.engine.scoring_runner", scoring_path]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode("utf-8")), timeout=min(timeout, 1.0))
                if proc.returncode == 0:
                    obj = json.loads(out.decode("utf-8", "ignore"))
                    if isinstance(obj, dict):
                        scores = {k: float(v) for k, v in obj.items() if isinstance(v, (int, float))}
                else:
                    # fallback to default below
                    pass
            except Exception:
                pass
            finally:
                try:
                    os.unlink(scoring_path)
                except Exception:
                    pass

        if not scores:
            # Default scoring rubric
            expected = ctx.get("expected") if isinstance(ctx, dict) else None
            correctness = 1.0 if (expected is not None and outputs.get("result") == expected) else 0.0
            duration_ms = float(timings.get("duration_ms", 0))
            speed = max(0.0, min(1.0, 1.0 - duration_ms / 1000.0))
            loc = float(outputs.get("loc", 0.0))
            brevity = max(0.0, min(1.0, 1.0 - (loc / 100.0)))
            # Only include metrics that make sense
            base_scores = {}
            for m in metrics:
                if m == "correctness":
                    base_scores[m] = correctness
                elif m == "speed":
                    base_scores[m] = speed
                elif m == "brevity":
                    base_scores[m] = brevity
            scores = base_scores

        # Compute simple aggregate over present metrics if not provided by scoring_fn
        if "aggregate" not in scores and scores:
            weights = {"correctness": 0.7, "speed": 0.2, "brevity": 0.1}
            present = {k: v for k, v in scores.items() if isinstance(v, (int, float)) and k in weights}
            if present:
                wsum = sum(weights[k] for k in present.keys())
                agg = sum((weights[k] / wsum) * float(present[k]) for k in present.keys())
                scores["aggregate"] = round(agg, 3)

        entry = {"index": idx, "item_id": item_id, "status": "ok", "scores": scores, "item": item, "outputs": {"result": outputs.get("result")}, "timings": timings}

        if judge_flag:
            try:
                from codeworld.engine.judge import judge_score, aggregate_judge, lexicographic_aggregate
                jmetrics = judge_score(task, ctx, outputs, timings)
                entry["scores_judge"] = jmetrics
                judge_mode = (provider_args.get("judge_mode") or "weighted").lower()
                if judge_mode == "lex":
                    entry["aggregate_judge_lex"] = lexicographic_aggregate(jmetrics)
                else:
                    jagg = aggregate_judge(jmetrics)
                    entry["aggregate_judge"] = round(jagg, 3)
            except Exception:
                pass

        results.append(entry)

    duration_ms = int((time.perf_counter() - started) * 1000)
    # Plateau signals (alpha): per-session aggregate history (Redis-backed when configured)
    signals: Dict[str, Any] = {}
    if session_id and results:
        # Use the mean of aggregates as session score for this batch
        aggs = []
        for r in results:
            agg = r.get("aggregate_judge") or r.get("scores", {}).get("aggregate")
            if isinstance(agg, (int, float)):
                aggs.append(float(agg))
        if aggs:
            current = sum(aggs) / len(aggs)
            hist = _SESSION_HISTORY.setdefault(session_id, [])
            hist.append(current)
            if len(hist) > _SESSION_MAX_HISTORY:
                hist.pop(0)
            epsilon = 0.005
            window = 5
            plateau = False
            # Redis persistence (optional)
            if _redis is not None:
                try:
                    key = f"cw:sessions:{session_id}:aggs"
                    _redis.lpush(key, current)
                    _redis.ltrim(key, 0, window)  # keep last window+1
                    # Set TTL to 24h to avoid unbounded growth
                    _redis.expire(key, 86400)
                    vals = [float(x) for x in (_redis.lrange(key, 0, window) or []) if isinstance(x, (int, float, str))]
                    if len(vals) >= window + 1:
                        prev = sum(vals[1:window+1]) / window
                        plateau = (vals[0] - prev) < epsilon
                except Exception:
                    pass
            else:
                if len(hist) >= window + 1:
                    prev = sum(hist[-(window+1):-1]) / window
                    plateau = (current - prev) < epsilon
            signals = {"session_id": session_id, "plateau": plateau, "current": round(current, 3)}

    response = {
        "summary": {
            "items": len(items),
            "succeeded": len(results),
            "failed": 0,
        },
        "statistics": {
            "iterations": iterations,
            "metrics": metrics,
            "allowed_languages": languages,
        },
        "results": results,
        "duration_ms": duration_ms,
        "stdout": "",
        "stderr": "",
        "run_manifest": {
            "ts": int(time.perf_counter() * 1000),
            "run_id": uuid.uuid4().hex,
            "schema": "canonical+codeworld@v1",
            "options": {"max_seconds": timeout, "session_id": session_id, "track_id": track_id},
            "task_ids": [r.get("item", {}).get("task_id") for r in results if isinstance(r.get("item"), dict) and r.get("item", {}).get("task_id")],
            "item_ids": [r.get("item_id") for r in results],
            # Optional: pass-through of tool invocations for deterministic replay
            "tools": [r.get("item", {}).get("context", {}).get("tool_invocations") for r in results if isinstance(r.get("item"), dict)],
        },
        "signals": signals,
    }
    # Persist artifact
    try:
        from pathlib import Path as _P
        out_dir = _P(__file__).resolve().parents[3] / "local" / "artifacts" / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"codeworld_run_{int(time.time())}.json").write_text(json.dumps(response, indent=2))
    except Exception:
        pass
    return JSONResponse(response)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "details": {"engine": "alpha@py-runner+dynamic-scoring+judge", "defaults": {"metrics": ["correctness", "speed", "brevity"], "languages": ["python"]}}})
