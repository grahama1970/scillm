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

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FLAGS = ["--deterministic", "--no-llm"]
DEFAULT_TIMEOUT = float(os.getenv("LEAN4_BRIDGE_TIMEOUT_SECONDS", "300"))

app = FastAPI(
    title="Lean4 Bridge",
    description="Minimal Lean4 batch endpoint compatible with LiteLLM bridge calls.",
    version="0.1.0",
)


class Lean4BridgeRequest(BaseModel):
    messages: List[Dict[str, Any]] = Field(..., description="Conversation context driving the request")
    lean4_requirements: List[Dict[str, Any]] = Field(..., description="Batch of requirements to prove")
    lean4_flags: List[str] | None = Field(None, description="Additional CLI flags")
    max_seconds: float | None = Field(
        None,
        description="Optional wall-clock limit (seconds) for the Lean4 batch run.",
    )


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
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
    requirements = _normalise_requirements(req.lean4_requirements)
    flags = req.lean4_flags or DEFAULT_FLAGS
    timeout = req.max_seconds or DEFAULT_TIMEOUT

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

    response = {
        "summary": {
            "items": len(proof_results) if isinstance(proof_results, list) else None,
            "proved": stats.get("successful_proofs"),
            "failed": stats.get("failed_proofs"),
            "unproved": stats.get("unproved"),
        },
        "statistics": stats,
        "proof_results": proof_results,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
        "duration_ms": duration_ms,
    }
    return JSONResponse(response)
