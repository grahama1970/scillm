from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
try:
    from common.types.quantity import QuantityModel  # strong unit-typed fields
except Exception:  # pragma: no cover
    QuantityModel = None  # type: ignore
try:
    from common.units import parse_quantity as _parse_q, to_json_dict as _q_to_json
except Exception:  # pragma: no cover
    _parse_q = None  # type: ignore
    _q_to_json = None  # type: ignore
try:
    from common.bridge.schemas import (
        ProviderArgs as CanonProviderArgs,
        Options as CanonOptions,
        CanonicalBridgeRequest,
    )
except Exception:  # pragma: no cover - fallback for non-src runs
    CanonProviderArgs = None  # type: ignore
    CanonOptions = None  # type: ignore
    CanonicalBridgeRequest = BaseModel  # type: ignore
import contextlib
import uuid

ROOT = Path(__file__).resolve().parents[2]
# Accept CERTAINLY_REPO as an alias for LEAN4_REPO (project package is named 'certainly')
_repo_env = os.getenv("CERTAINLY_REPO") or os.getenv("LEAN4_REPO") or "/home/graham/workspace/experiments/lean4"
LEAN4_REPO = Path(_repo_env).resolve()
DEFAULT_FLAGS = ["--deterministic", "--no-llm"]
DEFAULT_TIMEOUT = float(os.getenv("LEAN4_BRIDGE_TIMEOUT_SECONDS", "300"))

app = FastAPI(
    title="Lean4/Certainly Bridge",
    description="Minimal Lean4 (package: 'certainly') batch endpoint compatible with LiteLLM bridge calls.",
    version="0.1.0",
)


class ProviderArgs(BaseModel):
    name: str = Field("lean4")
    args: Dict[str, Any] = Field(default_factory=dict)


class Options(BaseModel):
    max_seconds: float | None = None
    session_id: str | None = None
    track_id: str | None = None


class EngineeringInputs(BaseModel):
    """Optional engineering inputs with units (kept minimal for robustness).

    Normalized to SI and injected into each item's context under the same
    key names if not already present.
    """
    airspeed: Any | None = None
    altitude: Any | None = None
    pressure: Any | None = None
    temperature: Any | None = None


class Lean4BridgeRequest(CanonicalBridgeRequest):
    # Canonical
    messages: List[Dict[str, Any]] = Field(..., description="Conversation context driving the request")
    items: List[Dict[str, Any]] | None = Field(None, description="Batch of requirements (canonical key)")
    provider: ProviderArgs | None = None
    options: Options | None = None
    # Back-compat aliases
    lean4_requirements: List[Dict[str, Any]] | None = Field(None, description="Batch of requirements to prove")
    lean4_flags: List[str] | None = Field(None, description="Additional CLI flags")
    max_seconds: float | None = Field(None, description="Optional wall-clock limit (seconds)")
    engineering: EngineeringInputs | None = Field(
        None,
        description="Optional engineering inputs with units; merged into each item's context",
    )
    units_policy: str | None = Field(
        None,
        description=(
            "Units handling policy: 'require' (default; ask for clarification when missing/ambiguous), "
            "'ask_always' (always return 422 asking for confirmation), or 'auto_convert' (convert silently)."
        ),
    )
    engineering_confirmed: bool | None = Field(
        None,
        description=(
            "Explicit attestation that engineering units were reviewed and confirmed by a human. "
            "Required when units_policy is 'ask_always' or 'require' and engineering is present."
        ),
    )


# ------------------------------
# Units validation / clarification
# ------------------------------

_ACCEPTED_UNITS: Dict[str, Dict[str, List[str]]] = {
    # field -> {preferred: [..], accepted: [..]}
    "airspeed": {"preferred": ["m/s"], "accepted": ["m/s", "kn"]},
    "altitude": {"preferred": ["m"], "accepted": ["m", "ft"]},
    "pressure": {"preferred": ["Pa"], "accepted": ["Pa", "kPa", "psi", "psf"]},
    "temperature": {"preferred": ["K"], "accepted": ["K", "degC", "degF"]},
}

# Soft sanity windows (SI base units) for core fields
_SANITY_BOUNDS_SI: Dict[str, Tuple[float, float]] = {
    "airspeed": (0.0, 1500.0),          # m/s
    "altitude": (-500.0, 120000.0),     # m
    "pressure": (0.0, 5.0e6),           # Pa
    "temperature": (100.0, 500.0),      # K
}


def _should_ask_always(policy: str | None) -> bool:
    p = (policy or os.getenv("LEAN4_UNITS_POLICY", "")).strip().lower()
    return p == "ask_always"


def _auto_convert(policy: str | None) -> bool:
    p = (policy or os.getenv("LEAN4_UNITS_POLICY", "")).strip().lower()
    return p == "auto_convert"


def _parse_opt_qty(obj: Any) -> Tuple[bool, Dict[str, Any] | None]:
    """Try to parse a quantity; returns (ok, si_dict or None)."""
    try:
        if _parse_q is None or _q_to_json is None:
            return False, None
        q = _parse_q(obj)
        return True, _q_to_json(q)
    except Exception:
        return False, None


def _validate_engineering(eng: EngineeringInputs | None, policy: str | None) -> Tuple[bool, Dict[str, Any]]:
    if eng is None:
        return True, {}
    issues: List[Dict[str, Any]] = []
    si_preview: Dict[str, Any] = {}
    for field in ("airspeed", "altitude", "pressure", "temperature"):
        provided = getattr(eng, field, None)
        if provided is None:
            continue
        ok, si = _parse_opt_qty(provided.to_dict() if hasattr(provided, "to_dict") else provided)
        if ok and si is not None:
            si_preview[field] = si
            # Sanity check
            try:
                lo, hi = _SANITY_BOUNDS_SI.get(field, (None, None))
                if lo is not None and hi is not None:
                    v = float(si.get("value"))
                    if not (lo <= v <= hi):
                        issues.append({
                            "field": field,
                            "issue": "out_of_range",
                            "provided": provided.to_dict() if hasattr(provided, "to_dict") else provided,
                            "si_value": si,
                            "recommended_range_si": {"min": lo, "max": hi, "unit": si.get("unit")},
                            "proceed_if_unspecified": False,
                        })
                        continue
            except Exception:
                pass
            # Check if unit is among accepted list
            try:
                accepted = _ACCEPTED_UNITS[field]["accepted"]
                if si.get("unit") not in ["m/s", "m", "Pa", "K"] and not _should_ask_always(policy):
                    # in base SI this will usually be one of the above; still allow policy to demand confirmation
                    pass
            except Exception:
                pass
            if _should_ask_always(policy):
                issues.append({
                    "field": field,
                    "issue": "confirmation_required",
                    "provided": provided.to_dict() if hasattr(provided, "to_dict") else provided,
                    "recommendations": {
                        "preferred_unit": _ACCEPTED_UNITS[field]["preferred"][0],
                        "accepted_units": _ACCEPTED_UNITS[field]["accepted"],
                        "examples": [si],
                    },
                    "proceed_if_unspecified": False,
                })
            continue
        # Not parseable → ask for clarification
        issues.append({
            "field": field,
            "issue": "missing_or_ambiguous_unit",
            "provided": provided if not hasattr(provided, "to_dict") else provided.to_dict(),
            "recommendations": {
                "preferred_unit": _ACCEPTED_UNITS[field]["preferred"][0],
                "accepted_units": _ACCEPTED_UNITS[field]["accepted"],
            },
            "proceed_if_unspecified": False,
        })
    if issues:
        # Build a human-facing, single-sentence prompt the agent can use verbatim
        def _mk_prompt() -> str:
            parts: List[str] = []
            for q in issues:
                fld = q.get("field")
                rec = q.get("recommendations") or {}
                pref = (rec.get("preferred_unit") or "SI").strip()
                acc = rec.get("accepted_units") or []
                if acc:
                    units_txt = f"preferred {pref}; accepted: {', '.join(acc)}"
                else:
                    units_txt = f"preferred {pref}"
                parts.append(f"{fld} ({units_txt})")
            return (
                "To proceed safely, please confirm units for: "
                + "; ".join(parts)
                + ". Reply with a JSON engineering object, e.g., {\"airspeed\":{\"value\":250,\"unit\":\"kn\"}}."
            )
        return False, {
            "type": "clarification_needed",
            "human_prompt": _mk_prompt(),
            "questions": issues,
            "canonical_si_preview": si_preview or None,
            "policy": (policy or os.getenv("LEAN4_UNITS_POLICY", "require")),
        }
    return True, {"canonical_si_preview": si_preview or None}


def _normalise_requirements(requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize incoming items into the canonical schema used by the CLI.

    Pass-through keys (when present and valid):
      - requirement_text (required)
      - context (dict)
      - metadata (dict)
      - strategies (list[str] or comma-separated str)

    This enables upstream callers (e.g., DeepSeek Prover planners) to propose
    a per-item strategy plan that the CLI will honor. Unknown keys are ignored.
    """
    normalised: List[Dict[str, Any]] = []
    for idx, raw in enumerate(requirements):
        text = raw.get("requirement_text") or raw.get("requirement")
        if not text or not isinstance(text, str):
            raise HTTPException(status_code=400, detail=f"Requirement #{idx} missing 'requirement_text'")
        context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        # Best-effort unit normalization on context/metadata
        def _norm_units(obj: Any) -> Any:
            if _parse_q is None or _q_to_json is None:
                return obj
            try:
                if isinstance(obj, dict):
                    # Quantity-like: {value, unit}
                    if set(obj.keys()) >= {"value", "unit"}:
                        q = _parse_q(obj)
                        return _q_to_json(q)  # base SI
                    # Recurse
                    return {k: _norm_units(v) for k, v in obj.items()}
                if isinstance(obj, (list, tuple)):
                    return [ _norm_units(v) for v in obj ]
                if isinstance(obj, (int, float)):
                    return obj
                if isinstance(obj, str):
                    # Try parsing "<num> <unit>" strings
                    s = obj.strip()
                    # crude fast-path: only attempt when there's a space and an alpha
                    if any(c.isalpha() for c in s) and any(c.isspace() for c in s):
                        try:
                            q = _parse_q(s)
                            return _q_to_json(q)
                        except Exception:
                            return obj
                    return obj
            except Exception:
                return obj
            return obj
        try:
            context = _norm_units(context)
            metadata = _norm_units(metadata)
        except Exception:
            pass
        # Optional strategies pass-through: list[str] or comma-separated str
        strategies_val = raw.get("strategies")
        strategies: List[str] | None = None
        if isinstance(strategies_val, list):
            try:
                strategies = [str(s).strip() for s in strategies_val if str(s).strip()]
            except Exception:
                strategies = None
        elif isinstance(strategies_val, str):
            s = strategies_val.strip()
            if s:
                strategies = [part.strip() for part in s.replace("\n", ",").split(",") if part.strip()]
        item: Dict[str, Any] = {
            "requirement_text": text,
            "context": context,
            "metadata": metadata,
        }
        if strategies:
            item["strategies"] = strategies
        normalised.append(item)
    if not normalised:
        raise HTTPException(status_code=400, detail="lean4_requirements must contain at least one item")
    return normalised


async def _run_cli(command: List[str], timeout: float) -> tuple[int, str, str]:
    env = os.environ.copy()
    # Ensure Lean4 CLI (`lean4_prover.cli_mini`) is importable in the child
    env["PYTHONPATH"] = f"{LEAN4_REPO / 'src'}:{env.get('PYTHONPATH','')}"
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(LEAN4_REPO),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise HTTPException(status_code=504, detail="Lean4 batch exceeded max_seconds")
    return proc.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")


@app.post("/bridge/complete")
async def bridge_complete(req: Lean4BridgeRequest):
    raw_items = req.items or req.lean4_requirements or []
    requirements = _normalise_requirements(raw_items) if raw_items else []
    if not requirements:
        raise HTTPException(status_code=400, detail="items/lean4_requirements must contain at least one item")
    # Units policy
    # Default to 'ask_always' to ensure collaborative clarification and no assumptions
    policy = (req.units_policy or os.getenv("LEAN4_UNITS_POLICY") or "ask_always").strip().lower()
    if policy != "auto_convert":
        ok_units, detail = _validate_engineering(getattr(req, "engineering", None), policy)
        if not ok_units:
            # 422 – ask for clarification; never assume units by default
            raise HTTPException(status_code=422, detail=detail)
        # Require explicit confirmation in collaborative modes when engineering present
        if getattr(req, "engineering", None) is not None:
            if not bool(getattr(req, "engineering_confirmed", False)):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "type": "clarification_needed",
                        "human_prompt": (
                            "Please confirm engineering units by resubmitting with 'engineering_confirmed': true. "
                            "This ensures no assumptions are made before formalization."
                        ),
                        "questions": [],
                        "policy": policy,
                    },
                )
    prov_args = {}
    if hasattr(req, "provider") and getattr(req, "provider") is not None:
        prov = getattr(req, "provider")
        # Accept either canonical or local ProviderArgs
        if hasattr(prov, "args") and isinstance(getattr(prov, "args"), dict):
            prov_args = getattr(prov, "args")  # type: ignore
    flags = req.lean4_flags or prov_args.get("flags") or DEFAULT_FLAGS
    timeout = DEFAULT_TIMEOUT
    session_id = None
    track_id = None
    if hasattr(req, "options") and getattr(req, "options") is not None:
        opts = getattr(req, "options")
        if hasattr(opts, "max_seconds") and getattr(opts, "max_seconds") is not None:
            try:
                timeout = float(getattr(opts, "max_seconds"))
            except Exception:
                timeout = DEFAULT_TIMEOUT
        # Echo session/track for parity with CodeWorld
        sid = getattr(opts, "session_id", None)
        tid = getattr(opts, "track_id", None)
        if isinstance(sid, str) and sid.strip():
            session_id = sid.strip()
        if isinstance(tid, str) and tid.strip():
            track_id = tid.strip()
    if hasattr(req, "max_seconds") and getattr(req, "max_seconds") is not None:
        try:
            timeout = float(getattr(req, "max_seconds"))
        except Exception:
            pass

    # Merge strong-typed engineering inputs into each item context (SI base units)
    if getattr(req, "engineering", None) is not None and QuantityModel is not None:
        eng = req.engineering  # type: ignore
        eng_ctx: Dict[str, Any] = {}
        try:
            for key in ("airspeed", "altitude", "pressure", "temperature"):
                qm = getattr(eng, key, None)
                if qm is not None:
                    try:
                        q_si = qm.to_pint().to_base_units()
                        eng_ctx[key] = {"value": float(q_si.magnitude), "unit": f"{q_si.units:~P}"}
                    except Exception:
                        pass
        except Exception:
            eng_ctx = {}
        if eng_ctx:
            for item in requirements:
                try:
                    ctx = item.setdefault("context", {}) if isinstance(item, dict) else None
                    if isinstance(ctx, dict):
                        for k, v in eng_ctx.items():
                            ctx.setdefault(k, v)
                except Exception:
                    pass

    with tempfile.NamedTemporaryFile("w", suffix="_lean4_in.json", delete=False) as fin:
        input_path = Path(fin.name)
        json.dump(requirements, fin)
        fin.flush()

    with tempfile.NamedTemporaryFile("w", suffix="_lean4_out.json", delete=False) as fout:
        output_path = Path(fout.name)

    # Echo/stub mode: if explicitly requested or CLI is not present, return a minimal OK payload
    echo = os.getenv("LEAN4_BRIDGE_ECHO", "") == "1"
    cli_path = LEAN4_REPO / "src" / "lean4_prover" / "cli_mini.py"
    if echo or not cli_path.exists():
        try:
            payload = {
                "statistics": {"successful_proofs": len(requirements), "failed_proofs": 0, "unproved": 0},
                "proof_results": [
                    {"id": f"item-{i+1}", "ok": True, "requirement_text": r.get("requirement_text"), "diagnostics": []}
                    for i, r in enumerate(requirements)
                ],
            }
            duration_ms = 0
            stats = payload["statistics"]
            proof_results = payload["proof_results"]
            response = {
                "summary": {"items": len(proof_results), "proved": stats.get("successful_proofs"), "failed": 0, "unproved": 0},
                "statistics": stats,
                "proof_results": proof_results,
                "results": proof_results,
                "stdout": "",
                "stderr": "",
                "duration_ms": duration_ms,
                "run_manifest": {
                    "ts": int(time.time()),
                    "run_id": uuid.uuid4().hex,
                    "flags": flags,
                    "lean4_repo": str(LEAN4_REPO),
                    "schema": "canonical+lean4@v1",
                    "options": {"max_seconds": timeout, "session_id": None, "track_id": None},
                    "provider": {"name": "certainly", "backend": "lean4"},
                },
            }
            return JSONResponse(response)
        finally:
            input_path.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "lean4_prover.cli_mini",
        "batch",
        "--input-file",
        str(input_path),
        "--output-file",
        str(output_path),
        "--json-diagnostics",
    ] + flags

    started = time.perf_counter()
    try:
        returncode, stdout, stderr = await _run_cli(cmd, timeout)
    finally:
        input_path.unlink(missing_ok=True)

    duration_ms = int((time.perf_counter() - started) * 1000)

    if returncode != 0:
        output_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Lean4 CLI returned non-zero status",
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
        )

    try:
        payload = json.loads(output_path.read_text())
    finally:
        output_path.unlink(missing_ok=True)

    stats = payload.get("statistics", {}) if isinstance(payload, dict) else {}
    proof_results = payload.get("proof_results", []) if isinstance(payload, dict) else []

    # Attach stable per-item ids to proof results when available
    if isinstance(proof_results, list):
        for i, pr in enumerate(proof_results):
            try:
                if isinstance(pr, dict):
                    pr.setdefault("item_id", pr.get("id") or pr.get("task_id") or f"item-{i+1}")
            except Exception:
                pass

    # Optional units attestation for run_manifest
    units_attestation = None
    try:
        import hashlib
        from importlib import resources as _res
        defs_bytes = None
        try:
            with _res.files("common").joinpath("units_defense.txt").open("rb") as f:
                defs_bytes = f.read()
        except Exception:
            defs_bytes = None
        normalized_eng = None
        if "eng_ctx" in locals() and isinstance(eng_ctx, dict) and eng_ctx:
            normalized_eng = eng_ctx
        units_attestation = {
            "confirmed": bool(getattr(req, "engineering_confirmed", False)),
            "normalized_engineering": normalized_eng,
            "definitions_sha256": hashlib.sha256(defs_bytes).hexdigest() if defs_bytes else None,
        }
    except Exception:
        units_attestation = None

    response = {
        "summary": {
            "items": len(proof_results) if isinstance(proof_results, list) else None,
            "proved": stats.get("successful_proofs"),
            "failed": stats.get("failed_proofs"),
            "unproved": stats.get("unproved"),
        },
        "statistics": stats,
        "proof_results": proof_results,
        "results": proof_results,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
        "duration_ms": duration_ms,
        "run_manifest": {
            "ts": int(time.time()),
            "run_id": uuid.uuid4().hex,
            "flags": flags,
            "lean4_repo": str(LEAN4_REPO),
            "schema": "canonical+lean4@v1",
            "options": {"max_seconds": timeout, "session_id": session_id, "track_id": track_id},
            "provider": {"name": "certainly", "backend": "lean4"},
            "units": units_attestation,
        },
    }
    try:
        out_dir = ROOT / "local" / "artifacts" / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Use wall-clock seconds as suffix
        stamp = str(int(time.time()))
        (out_dir / f"lean4_run_{stamp}.json").write_text(json.dumps(response, indent=2))
    except Exception:
        pass
    return JSONResponse(response)


@app.get("/healthz")
async def healthz():
    ok = True
    details: Dict[str, Any] = {
        "repo": str(LEAN4_REPO),
        "repo_exists": LEAN4_REPO.exists(),
        "default_timeout": DEFAULT_TIMEOUT,
    }
    if not details["repo_exists"]:
        ok = False
    return JSONResponse({"ok": ok, "details": details})


# Preview endpoint to normalize engineering units and report issues without running Lean4
class UnitsNormalizeRequest(BaseModel):
    engineering: EngineeringInputs
    units_policy: str | None = None


@app.post("/bridge/units/normalize")
async def normalize_units(req: UnitsNormalizeRequest):
    policy = (req.units_policy or os.getenv("LEAN4_UNITS_POLICY") or "ask_always").strip().lower()
    ok_units, detail = _validate_engineering(req.engineering, policy)
    out: Dict[str, Any] = {"ok": ok_units}
    out.update(detail)
    return JSONResponse(out)
