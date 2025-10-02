from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
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


def _normalise_requirements(requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalised: List[Dict[str, Any]] = []
    for idx, raw in enumerate(requirements):
        text = raw.get("requirement_text") or raw.get("requirement")
        if not text or not isinstance(text, str):
            raise HTTPException(status_code=400, detail=f"Requirement #{idx} missing 'requirement_text'")
        context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        normalised.append(
            {
                "requirement_text": text,
                "context": context,
                "metadata": metadata,
            }
        )
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

    with tempfile.NamedTemporaryFile("w", suffix="_lean4_in.json", delete=False) as fin:
        input_path = Path(fin.name)
        json.dump(requirements, fin)
        fin.flush()

    with tempfile.NamedTemporaryFile("w", suffix="_lean4_out.json", delete=False) as fout:
        output_path = Path(fout.name)

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
