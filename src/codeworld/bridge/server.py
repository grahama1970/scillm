from __future__ import annotations

import time
import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import asyncio
import tempfile
import os
import sys
import shutil
import uuid
import logging
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
    version="0.2.1",
)

# Minimal in-process session history for plateau signals (alpha).
# Not durable across restarts; use Redis in production.
_SESSION_HISTORY: Dict[str, List[float]] = {}
_SESSION_MAX_HISTORY = 50

# Debug toggle (mirrors SCILLM_DEBUG)
_DEBUG = str(os.getenv("CODEWORLD_DEBUG", os.getenv("SCILLM_DEBUG", ""))).lower() in {"1", "true", "yes"}


def _dbg(msg: str) -> None:
    if _DEBUG:
        try:
            print(f"[codeworld.bridge][debug] {msg}")
        except Exception:
            pass


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
AUTOGEN_DEFAULT_N = int(os.getenv("CODEWORLD_MCTS_AUTO_N", "3") or "3")
AUTOGEN_DEFAULT_TEMPERATURE = float(os.getenv("CODEWORLD_MCTS_AUTO_TEMPERATURE", "0.0") or "0.0")
AUTOGEN_DEFAULT_MAX_TOKENS = int(os.getenv("CODEWORLD_MCTS_AUTO_MAX_TOKENS", "2000") or "2000")
AUTOGEN_HTTP_TIMEOUT_S = float(os.getenv("CODEWORLD_AUTOGEN_HTTP_TIMEOUT_S", "45") or "45")
_redis = None
if REDIS_URL:
    try:
        import redis  # type: ignore
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _redis = None

@app.post("/bridge/complete")
async def bridge_complete(req: CodeWorldBridgeRequest, request: Request, x_trace_id: str | None = Header(default=None)):
    started = time.perf_counter()
    response: Dict[str, Any] = {}
    trace = x_trace_id or request.headers.get("x-trace-id") or "-"
    _dbg(f"bridge_complete begin trace_id={trace}")

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
    _dbg(f"items={len(items)} metrics={metrics} iterations={iterations} languages={languages}")
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

    # Strategy detection (supports provider.args.strategy or strategy_config.name)
    strategy_name = ""
    try:
        _cfg = provider_args.get("strategy_config") if isinstance(provider_args, dict) else None
        strategy_name = (provider_args.get("strategy") or (_cfg.get("name") if isinstance(_cfg, dict) else "") or "").lower()
    except Exception:
        strategy_name = ""

    # exploration_constant alias normalization (provider-level)
    try:
        if isinstance(provider_args, dict):
            cfg = provider_args.get("strategy_config") if isinstance(provider_args.get("strategy_config"), dict) else {}
            if "uct_c" not in cfg and "exploration_constant" in cfg:
                cfg["uct_c"] = cfg.get("exploration_constant")
                provider_args["strategy_config"] = cfg
            elif "uct_c" in cfg and "exploration_constant" in cfg and cfg["uct_c"] != cfg["exploration_constant"]:
                logging.warning("[codeworld][warn] exploration_constant and uct_c differ; using uct_c (canonical)")
    except Exception:
        pass

    results = []
    for idx, item in enumerate(items):
        task = (item.get("task") or item.get("spec") or "").strip()
        ctx = item.get("context") or {}
        # Stable per-item id for replay
        item_id = item.get("task_id") or item.get("id") or f"item-{idx+1}"

        # Alpha runner: Python strategy variants (optional), else simulate an output
        code_variants = (ctx.get("code_variants") or {}) if isinstance(ctx, dict) else {}
        # If MCTS requested but no variants provided, attempt autogeneration via codex-agent
        if strategy_name == "mcts" and (not code_variants) and isinstance(provider_args, dict):
            try:
                cfg = provider_args.get("strategy_config") if isinstance(provider_args.get("strategy_config"), dict) else {}
                autogen = cfg.get("autogenerate", provider_args.get("autogenerate"))
                enabled = (autogen is True) or (isinstance(autogen, dict) and autogen.get("enabled") is True)
                env_gate = str(os.getenv("CODEWORLD_ENABLE_MCTS_GENERATE", "1")).lower()
                gate_allowed = env_gate not in ("0", "false", "no")
                if enabled and gate_allowed:
                    t_autogen = time.perf_counter()
                    # Prefer structured autogen dict when present, else check top-level args for n_variants, temperature, etc.
                    n = int((autogen.get("n") if isinstance(autogen, dict) else provider_args.get("n_variants") or AUTOGEN_DEFAULT_N) or AUTOGEN_DEFAULT_N)
                    gen_model = str((autogen.get("generator_model") if isinstance(autogen, dict) else provider_args.get("generator_model") or os.getenv("CODEX_AGENT_MODEL", "gpt-5")) or "gpt-5")
                    temperature = float((autogen.get("temperature") if isinstance(autogen, dict) else provider_args.get("temperature") or AUTOGEN_DEFAULT_TEMPERATURE) or AUTOGEN_DEFAULT_TEMPERATURE)
                    max_tokens = int((autogen.get("max_tokens") if isinstance(autogen, dict) else provider_args.get("max_tokens") or AUTOGEN_DEFAULT_MAX_TOKENS) or AUTOGEN_DEFAULT_MAX_TOKENS)
                    prompt = _mcts_build_generation_prompt(task, ctx, n)
                    _dbg(f"autogen enabled n={n} model={gen_model} temp={temperature} max_tokens={max_tokens} trace_id={trace}")
                    llm = _mcts_call_llm_for_variants(prompt, n=n, model=gen_model, temperature=temperature, max_tokens=max_tokens)
                    raw = llm.get("raw", "")
                    _dbg(f"autogen raw_len={len(raw) if isinstance(raw, str) else 0} trace_id={trace}")
                    vars = _mcts_extract_variants_from_raw(raw)
                    _dbg(f"autogen parsed_variants={len(vars) if isinstance(vars, list) else 0} trace_id={trace}")
                    try:
                        # preview ids
                        if isinstance(vars, list):
                            ids = [v.get("id") for v in vars if isinstance(v, dict)]
                            _dbg(f"autogen ids={ids[:6]} trace_id={trace}")
                    except Exception:
                        pass
                    _dbg(f"autogen elapsed={(time.perf_counter()-t_autogen):.2f}s trace_id={trace}")
                    if isinstance(vars, list) and vars:
                        # Normalize to mapping id->code for engine
                        mapping = {}
                        for i, v in enumerate(vars, 1):
                            if not isinstance(v, dict):
                                continue
                            vid = v.get("id") or f"v{i}"
                            code = v.get("code") if isinstance(v.get("code"), str) else ""
                            mapping[vid] = code
                        if mapping:
                            if isinstance(ctx, dict):
                                ctx["code_variants"] = mapping
                                code_variants = mapping
            except Exception:
                pass
        outputs: Dict[str, Any] = {}
        timings: Dict[str, Any] = {}
        t0 = time.perf_counter()
        mcts_out = None
        if code_variants and strategy_name == "mcts":
            # Fast-path when network is disabled: pick a best variant deterministically and return immediately.
            if STRATEGY_NONET:
                try:
                    # Heuristic: shortest code wins; break ties lexicographically by id
                    best_id = sorted(code_variants.keys(), key=lambda k: (len(code_variants.get(k, "") or ""), str(k)))[0]
                    best_code = code_variants.get(best_id, "")
                except Exception:
                    # Fallback to first item order
                    best_id, best_code = next(iter(code_variants.items()))
                mcts_out = {
                    "best_variant": str(best_id),
                    "best_value": 1.0,  # nominal score in nonet mode
                    "rollouts": 0,
                    "depth": 0,
                    "uct_c": 1.4,
                    "visits": 0,
                    "explored": 0,
                    "seed": None,
                    "error": None,
                }
                outputs["result"] = mcts_out["best_value"]
                timings["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            else:
                # MCTS phase-1: deterministic pseudo-reward, no code execution
                try:
                    from codeworld.engine.mcts import run_mcts  # local import for optional dep
                except Exception:
                    run_mcts = None  # type: ignore
                if run_mcts is None:
                    outputs["result"] = _score_hash(json.dumps(ctx, sort_keys=True))
                    timings["duration_ms"] = int((time.perf_counter() - t0) * 1000)
                else:
                    cfg = provider_args.get("strategy_config") if isinstance(provider_args.get("strategy_config"), dict) else {}
                    # Top-level sugar fallbacks
                    rollouts = int(cfg.get("rollouts", provider_args.get("rollouts", 64))) if isinstance(provider_args, dict) else 64
                    depth_v = int(cfg.get("depth", provider_args.get("depth", 8))) if isinstance(provider_args, dict) else 8
                    uct_c_v = cfg.get("uct_c", provider_args.get("uct_c", 1.4)) if isinstance(provider_args, dict) else 1.4
                    if "exploration_constant" in (cfg or {}) and "uct_c" not in (cfg or {}):
                        uct_c_v = cfg.get("exploration_constant")
                    seed_v = (cfg.get("seed") if isinstance(cfg, dict) else None) or (provider_args.get("seed") if isinstance(provider_args, dict) else None)
                    timeout_ms_v = int(cfg.get("timeout_ms", provider_args.get("timeout_ms", 50))) if isinstance(provider_args, dict) else 50
                    _dbg(f"mcts rollouts={rollouts} depth={depth_v} uct_c={uct_c_v} seed={seed_v} timeout_ms={timeout_ms_v} trace_id={trace}")
                    try:
                        mcts_out = run_mcts(task=task, context=ctx, code_variants=code_variants, rollouts=rollouts, depth=depth_v, uct_c=float(uct_c_v), seed=seed_v, timeout_ms=timeout_ms_v)
                    except Exception as e:  # noqa: BLE001
                        logging.exception("run_mcts failed")
                        mcts_out = {"error": str(e), "rollouts": 0, "depth": depth_v, "uct_c": float(uct_c_v), "visits": 0, "explored": 0, "seed": None}
                    outputs["result"] = mcts_out.get("best_value")
                    timings["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        elif code_variants:
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
        # If we synthesized code variants (autogen), attach them so downstream judges/tools can see candidates
        try:
            if isinstance(code_variants, dict) and code_variants:
                entry["code_variants"] = [
                    {"id": str(k), "code": v} for k, v in list(code_variants.items())
                    if isinstance(k, (str, int)) and isinstance(v, str)
                ]
                _dbg(f"attach code_variants n={len(entry['code_variants'])} trace_id={trace}")
        except Exception:
            pass
        if mcts_out is not None:
            try:
                entry["mcts"] = {
                    "best_variant": mcts_out.get("best_variant"),
                    "best_value": mcts_out.get("best_value"),
                    "rollouts": mcts_out.get("rollouts"),
                    "depth": mcts_out.get("depth"),
                    "uct_c": mcts_out.get("uct_c"),
                    "visits": mcts_out.get("visits"),
                    "explored": mcts_out.get("explored"),
                    "seed": mcts_out.get("seed"),
                    "error": mcts_out.get("error"),
                }
            except Exception:
                pass

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
    # Mirror strategy info if MCTS was requested
    try:
        if strategy_name == "mcts":
            m0 = next((r.get("mcts") for r in results if isinstance(r, dict) and r.get("mcts")), None)
            if isinstance(m0, dict):
                _dbg(f"mcts best_variant={(m0.get('best_variant') if isinstance(m0.get('best_variant'), str) else 'obj')} best_value={m0.get('best_value')} trace_id={trace}")
                response["run_manifest"]["strategy_name"] = "mcts"
                response["run_manifest"]["strategy_seed"] = m0.get("seed")
                response["run_manifest"]["strategy_params"] = {k: m0.get(k) for k in ("rollouts", "depth", "uct_c")}
                # Add compact run-level mcts_stats for quick indexing
                _bv = m0.get("best_variant")
                _bv_id = None
                try:
                    if isinstance(_bv, dict):
                        _bv_id = _bv.get("id") or _bv.get("name")
                    elif isinstance(_bv, str):
                        _bv_id = _bv
                except Exception:
                    _bv_id = None
                response["run_manifest"]["mcts_stats"] = {
                    "rollouts": m0.get("rollouts"),
                    "depth": m0.get("depth"),
                    "uct_c": m0.get("uct_c"),
                    "visits": m0.get("visits"),
                    "explored": m0.get("explored"),
                    "best_value": m0.get("best_value"),
                    "best_variant": _bv_id,
                    "seed": m0.get("seed"),
                    "error": m0.get("error"),
                }
    except Exception:
        pass
    # Persist artifact
    try:
        from pathlib import Path as _P
        out_dir = _P(__file__).resolve().parents[3] / "local" / "artifacts" / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"codeworld_run_{int(time.time())}.json").write_text(json.dumps(response, indent=2))
    except Exception:
        pass
    _dbg(f"bridge_complete done dur={(time.perf_counter()-started):.2f}s items={len(items)} trace_id={trace}")
    return JSONResponse(response)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "details": {"engine": "alpha@py-runner+dynamic-scoring+judge", "defaults": {"metrics": ["correctness", "speed", "brevity"], "languages": ["python"]}}})


# ===== Optional: MCTS autogeneration helper path (unit-testable) =====

def _mcts_hash_text(s: str) -> str:
    try:
        import hashlib as _hh
        return _hh.sha256(s.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _mcts_build_generation_prompt(task: Any, context: Optional[Dict[str, Any]], n: int) -> str:
    try:
        ctx = json.dumps(context or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        ctx = repr(context)
    if isinstance(task, str):
        task_str = task
    else:
        try:
            task_str = json.dumps(task, ensure_ascii=False)
        except Exception:
            task_str = repr(task)
    return f"Generate exactly {n} JSON variants for the task below with fields id,title,complexity_tier,rationale,code,notes. Return STRICT JSON with top-level key 'variants'.\nTask:\n{task_str}\nContext:\n{ctx}"


def _mcts_call_llm_for_variants(prompt: str, *, n: int, model: str, temperature: float = 0.0, max_tokens: int = 2000,
                                 base_url: Optional[str] = None, api_key: Optional[str] = None) -> Dict[str, Any]:
    from urllib import request as _urlreq
    base = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("CODEX_AGENT_API_BASE") or "http://127.0.0.1:8089"
    key = api_key or os.getenv("OPENAI_API_KEY") or "none"
    if _DEBUG:
        try:
            print(f"[codeworld.bridge][debug] autogen base={base} model={model}")
        except Exception:
            pass
    payload = {"model": model, "messages": [{"role": "system", "content": "You produce strict JSON only."}, {"role": "user", "content": prompt}],
               "temperature": temperature, "max_tokens": max_tokens}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    for path in ("/v1/chat/completions", "/chat/completions"):
        url = base.rstrip("/") + path
        try:
            req = _urlreq.Request(url=url, data=data, headers=headers, method="POST")
            with _urlreq.urlopen(req, timeout=AUTOGEN_HTTP_TIMEOUT_S) as resp:
                rsp = json.loads(resp.read().decode("utf-8"))
            content = rsp.get("choices", [{}])[0].get("message", {}).get("content", "")
            _dbg(f"autogen call ok path={path} content_len={len(content) if isinstance(content, str) else 0}")
            return {"raw": content}
        except Exception as e:  # noqa: BLE001
            logging.warning("mcts variant llm call failed at %s: %s", url, e)
            _dbg(f"autogen call failed path={path} err={e}")
            continue
    return {"raw": ""}


def _mcts_extract_variants_from_raw(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except Exception:
        first, last = raw.find("{"), raw.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return []
        try:
            obj = json.loads(raw[first:last + 1])
        except Exception:
            return []
    variants = obj.get("variants", []) if isinstance(obj, dict) else []
    return variants if isinstance(variants, list) else []


def apply_mcts_strategy(entry: Dict[str, Any], provider_args: Optional[Dict[str, Any]], task: Any, context: Optional[Dict[str, Any]]) -> None:
    """
    Unit-testable helper that optionally autogenerates variants, then runs MCTS and
    populates entry["mcts"] and mirrors generator + strategy info into entry["run_manifest"].
    """
    if not isinstance(entry, dict):
        return
    args = provider_args or {}
    args_block = args.get("args") if isinstance(args.get("args"), dict) else args
    strategy = (args_block.get("strategy") or "").lower()
    if strategy != "mcts":
        return

    # Generator: only when enabled and code_variants empty
    cfg = args_block.get("strategy_config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    autogen = cfg.get("autogenerate", args_block.get("autogenerate"))
    enabled = False
    gen_cfg: Dict[str, Any] = {}
    if isinstance(autogen, bool):
        enabled = autogen
    elif isinstance(autogen, dict):
        enabled = bool(autogen.get("enabled", False))
        gen_cfg = dict(autogen)
    if "n_variants" in args_block:
        gen_cfg["n"] = int(args_block["n_variants"])  # type: ignore[index]
    if "generator_model" in args_block:
        gen_cfg["generator_model"] = args_block["generator_model"]
    if "temperature" in args_block:
        gen_cfg["temperature"] = args_block["temperature"]
    if "max_tokens" in args_block:
        gen_cfg["max_tokens"] = args_block["max_tokens"]

    env_gate = os.getenv("CODEWORLD_ENABLE_MCTS_GENERATE", "1").lower()
    gate_allowed = env_gate not in ("0", "false", "no")

    manifest = entry.get("run_manifest")
    if not isinstance(manifest, dict):
        manifest = {}
        entry["run_manifest"] = manifest

    gen_meta = {
        "enabled": bool(enabled),
        "skipped_by_env": not gate_allowed if enabled else False,
        "n": int(gen_cfg.get("n", AUTOGEN_DEFAULT_N)),
        "model": gen_cfg.get("generator_model", os.getenv("CODEX_AGENT_MODEL", "gpt-5")),
        "temperature": float(gen_cfg.get("temperature", AUTOGEN_DEFAULT_TEMPERATURE)),
        "max_tokens": int(gen_cfg.get("max_tokens", AUTOGEN_DEFAULT_MAX_TOKENS)),
        "prompt_hash": None,
        "response_hash": None,
        "error": None,
    }

    if enabled and gate_allowed and not entry.get("code_variants"):
        prompt = _mcts_build_generation_prompt(task, context, gen_meta["n"])  # type: ignore[arg-type]
        gen_meta["prompt_hash"] = _mcts_hash_text(prompt)
        try:
            llm = _mcts_call_llm_for_variants(
                prompt,
                n=gen_meta["n"],
                model=str(gen_meta["model"]),
                temperature=float(gen_meta["temperature"]),
                max_tokens=int(gen_meta["max_tokens"]),
            )
            raw = llm.get("raw", "")
            gen_meta["response_hash"] = _mcts_hash_text(raw) if raw else None
            variants = _mcts_extract_variants_from_raw(raw)
            norm: list[dict[str, Any]] = []
            for v in variants:
                if not isinstance(v, dict):
                    continue
                vid = v.get("id") or f"v{len(norm)+1}"
                norm.append({
                    "id": vid,
                    "title": v.get("title"),
                    "complexity_tier": v.get("complexity_tier"),
                    "rationale": v.get("rationale"),
                    "code": v.get("code") if isinstance(v.get("code"), str) else "",
                    "notes": v.get("notes"),
                })
            if norm:
                entry["code_variants"] = norm
            else:
                gen_meta["error"] = "generation_empty_or_unparseable"
        except Exception as e:  # noqa: BLE001
            logging.exception("mcts variant generation failed")
            gen_meta["error"] = str(e)

    manifest["strategy_generator"] = gen_meta

    # If no variants after generation, bail
    code_variants = entry.get("code_variants")
    if not code_variants:
        return

    # Map knobs and run engine
    depth_v = int(cfg.get("depth", args_block.get("depth", 8)))
    uct_c_v = cfg.get("uct_c", args_block.get("uct_c", 1.4))
    if "exploration_constant" in cfg and "uct_c" not in cfg:
        uct_c_v = cfg.get("exploration_constant")
    rollouts_v = int(cfg.get("rollouts", args_block.get("rollouts", 64)))
    seed_v = cfg.get("seed") or args_block.get("seed")
    timeout_ms_v = int(cfg.get("timeout_ms", args_block.get("timeout_ms", 50)))
    try:
        try:
            from src.codeworld.engine.mcts import run_mcts  # tests path
        except Exception:
            from codeworld.engine.mcts import run_mcts  # runtime path
        mcts_out = run_mcts(task=task, context=context, code_variants=code_variants, rollouts=rollouts_v, depth=depth_v, uct_c=float(uct_c_v), seed=seed_v, timeout_ms=timeout_ms_v)
    except Exception as e:  # noqa: BLE001
        logging.exception("run_mcts failed")
        mcts_out = {"error": str(e), "rollouts": 0, "depth": depth_v, "uct_c": float(uct_c_v), "visits": 0, "explored": 0, "seed": None}

    entry["mcts"] = {
        "best_variant": mcts_out.get("best_variant"),
        "best_value": mcts_out.get("best_value"),
        "rollouts": mcts_out.get("rollouts"),
        "depth": mcts_out.get("depth"),
        "uct_c": mcts_out.get("uct_c"),
        "visits": mcts_out.get("visits"),
        "explored": mcts_out.get("explored"),
        "seed": mcts_out.get("seed"),
        "error": mcts_out.get("error"),
    }
    manifest["strategy_name"] = "mcts"
    manifest["strategy_seed"] = mcts_out.get("seed")
    manifest["strategy_params"] = {"rollouts": rollouts_v, "depth": depth_v, "uct_c": float(uct_c_v), "timeout_ms": timeout_ms_v}
    # Run-level aggregate block for quick indexing
    # Derive a compact best_variant id when possible
    _best = mcts_out.get("best_variant")
    best_id = None
    try:
        if isinstance(_best, dict):
            best_id = _best.get("id") or _best.get("name")
        elif isinstance(_best, str):
            best_id = _best
    except Exception:
        best_id = None
    manifest["mcts_stats"] = {
        "rollouts": mcts_out.get("rollouts"),
        "depth": mcts_out.get("depth"),
        "uct_c": mcts_out.get("uct_c"),
        "visits": mcts_out.get("visits"),
        "explored": mcts_out.get("explored"),
        "best_value": mcts_out.get("best_value"),
        "best_variant": best_id,
        "seed": mcts_out.get("seed"),
        "error": mcts_out.get("error"),
    }
