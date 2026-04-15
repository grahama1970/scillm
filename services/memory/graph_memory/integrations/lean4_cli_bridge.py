#!/usr/bin/env python3
"""Lean4 bridge with pooled Graph Memory recall, local LRU cache, structured logging, and robust failure handling."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List

from loguru import logger as _loguru_logger

# ---------- logging (JSON + redaction) ----------
SECRET_KEYS = ("PASS", "KEY", "TOKEN", "SECRET")


def _redact_env(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        out[k] = "<redacted>" if any(s in k.upper() for s in SECRET_KEYS) else v
    return out


class _LogWrapper:
    """Thin wrapper around loguru logger that adds emit_json() for structured logging."""

    def __init__(self):
        self._json_mode = os.getenv("GM_LOG_JSON", "1") == "1"

    def emit_json(self, evt: Dict[str, Any]):
        if self._json_mode:
            _loguru_logger.info(json.dumps(evt, ensure_ascii=False))
        else:
            _loguru_logger.info("{}", evt)

    def __getattr__(self, name):
        return getattr(_loguru_logger, name)


log = _LogWrapper()

# ---------- normalization + LRU ----------
WS = re.compile(r"\s+")


def normalize_req(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("∑", "sum").replace("∀", "forall").replace("→", "->").replace("≤", "<=").replace("≥", ">=")
    return WS.sub(" ", s)


class LRU:
    def __init__(self, capacity: int = 512):
        self.capacity = capacity
        self._d: "OrderedDict[str, Any]" = OrderedDict()

    def get(self, k: str):
        if k in self._d:
            v = self._d.pop(k)
            self._d[k] = v
            return v
        return None

    def set(self, k: str, v: Any):
        if k in self._d:
            self._d.pop(k)
        elif len(self._d) >= self.capacity:
            self._d.popitem(last=False)
        self._d[k] = v


# ---------- GM recall (pooled) ----------
@dataclass
class Neighbor:
    id: str
    title: str = ""
    text: str = ""
    tags: List[str] = dataclasses.field(default_factory=list)
    score: float = 0.0


class GMRecall:
    def __init__(self):
        self.scope = os.getenv("GM_PROVER_SCOPE", "default")
        self.bm25_w = float(os.getenv("RECALL_BM25_WEIGHT", "0.6"))
        self.graph_w = float(os.getenv("RECALL_GRAPH_WEIGHT", "0.4"))
        self.dense_w = float(os.getenv("RECALL_DENSE_WEIGHT", "0.25"))
        self.use_dense = 1  # Always on — semantic search is non-negotiable
        self.model_id = os.getenv("GM_MODEL_ID")
        try:
            from graph_memory.lessons import recall as gm_recall  # late import

            self._recall_mod = gm_recall
        except Exception as exc:
            log.debug("graph_memory.lessons.recall import failed: {}", exc)
            self._recall_mod = None

    def available(self) -> bool:
        return self._recall_mod is not None

    def recall_one(self, query: str, top_k: int = 32) -> List[Neighbor]:
        if not self.available():
            return []
        try:
            fn = getattr(self._recall_mod, "recall", None)
            if fn is None:
                return []
            res = fn(
                query=query,
                scope=self.scope,
                bm25_w=self.bm25_w,
                graph_w=self.graph_w,
                dense_w=(self.dense_w if self.use_dense else 0.0),
                top_k=top_k,
                model_id=self.model_id,
            )
            out: List[Neighbor] = []
            for r in res or []:
                out.append(
                    Neighbor(
                        id=str(r.get("id", "")),
                        title=r.get("title", ""),
                        text=r.get("text", ""),
                        tags=r.get("tags", []) or [],
                        score=float(r.get("score", 0.0)),
                    )
                )
            return out
        except TypeError:
            # legacy positional signature
            res = self._recall_mod.recall(query, self.scope, self.bm25_w, self.graph_w, top_k)  # type: ignore[attr-defined]
            return [
                Neighbor(
                    id=str(r.get("id", "")),
                    title=r.get("title", ""),
                    text=r.get("text", ""),
                    tags=r.get("tags", []) or [],
                    score=float(r.get("score", 0.0)),
                )
                for r in res or []
            ]
        except Exception as e:
            log.emit_json({"lvl": "warn", "at": "gm.recall_one", "err": str(e)})
            return []

    def recall_pooled(self, uniq_queries: Dict[str, str], top_k: int = 32) -> Dict[str, List[Neighbor]]:
        return {k: self.recall_one(q, top_k=top_k) for k, q in uniq_queries.items()}


# ---------- neighbor→strategy ----------
def strategies_from_neighbors(req: str, neighbors: List[Neighbor]) -> List[str]:
    text = (req or "") + " " + " ".join((n.title + " " + n.text + " " + " ".join(n.tags)) for n in neighbors)
    tl = text.lower()
    S: List[str] = []
    if any(tok in tl for tok in ("finset", "range", "nat", "sum", "forall")):
        S += ["induction", "compute"]
    if any(tok in tl for tok in ("ring", "linarith", "semiring", "algebra")):
        S += ["ring", "linarith"]
    S += ["simp", "rewrite", "aesop", "library_search", "structured"]
    seen = set()
    uniq: List[str] = []
    for s in S:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    best_of = int(os.getenv("LEAN4_BEST_OF", "8"))
    return uniq[: max(1, best_of)]


# ---------- Lean runner with retries ----------
def _classify_error(stderr: str, return_code: int) -> str:
    s = (stderr or "").lower()
    if return_code in (125, 126, 127) or "docker" in s or "connection refused" in s or "oci runtime" in s:
        return "container"
    if "rate limit" in s or "openai" in s or "http" in s or "timeout" in s:
        return "llm"
    return "compile"


def run_lean(requirement: str, strategies: List[str], timeout_s: int = 120) -> Dict[str, Any]:
    cmd_str = os.getenv("LEAN4_CLI_ENTRY", "python -m lean4_prover run")
    args = shlex.split(cmd_str) + ["--best-of", str(len(strategies))]
    for s in strategies:
        args += ["--strategy", s]
    payload = {"goal": requirement}
    tries = int(os.getenv("LEAN4_MAX_RETRIES", "3"))
    backoff = float(os.getenv("LEAN4_BACKOFF_S", "0.5"))
    for attempt in range(1, tries + 1):
        t0 = time.time()
        try:
            p = subprocess.run(
                args,
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
            dt = round((time.time() - t0) * 1000)
            stdout = p.stdout.decode("utf-8", errors="replace")
            stderr = p.stderr.decode("utf-8", errors="replace")
            lean_code = None
            tactics_used = None
            assumptions = None
            try:
                j = json.loads(stdout)
                lean_code = j.get("lean_code")
                tactics_used = j.get("tactics_used")
                assumptions = j.get("assumptions")
            except Exception as exc:
                log.debug("lean stdout JSON parse failed: {}", exc)
                pass
            return {
                "stdout": stdout,
                "stderr": stderr,
                "return_code": p.returncode,
                "elapsed_ms": dt,
                "lean_code": lean_code,
                "tactics_used": tactics_used,
                "assumptions": assumptions,
            }
        except subprocess.TimeoutExpired:
            if attempt >= tries:
                return {"stdout": "", "stderr": "timeout", "return_code": 124}
        except Exception as e:
            err = str(e)
            err_type = "container" if ("No such file" in err or "docker" in err) else "unknown"
            if attempt >= tries or err_type != "container":
                return {"stdout": "", "stderr": err, "return_code": 1, "error_type": err_type}
        time.sleep(backoff)
        backoff *= 2.0
    return {"stdout": "", "stderr": "unknown", "return_code": 1}


# ---------- IO + main ----------
@dataclass
class InItem:
    id: str
    item_type: str
    requirement: str


def _read_inputs(stdin_jsonl: str | None) -> List[InItem]:
    lines: List[str] = []
    if stdin_jsonl and stdin_jsonl != "-":
        with open(stdin_jsonl, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f]
    else:
        for ln in sys.stdin:
            lines.append(ln.rstrip("\n"))
    items: List[InItem] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            j = json.loads(ln)
            items.append(
                InItem(
                    id=str(j.get("id", "")),
                    item_type=j.get("item_type", "requirement"),
                    requirement=j.get("requirement", "") or j.get("goal", ""),
                )
            )
        except Exception as exc:
            log.debug("input line JSON parse failed: {}", exc)
            items.append(InItem(id="", item_type="requirement", requirement=f"<nonjson:{ln[:80]}>"))
    return items


def _emit_result(obj: Dict[str, Any]):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "stdin_jsonl",
        nargs="?",
        default="-",
        help="JSONL path or '-' for stdin (Extractor passes {stdin_jsonl})",
    )
    args = ap.parse_args()

    cfg = _redact_env(
        {
            "GM_PROVER_SCOPE": os.getenv("GM_PROVER_SCOPE", "default"),
            "RECALL_BM25_WEIGHT": os.getenv("RECALL_BM25_WEIGHT", "0.6"),
            "RECALL_GRAPH_WEIGHT": os.getenv("RECALL_GRAPH_WEIGHT", "0.4"),
            "RECALL_USE_DENSE": os.getenv("RECALL_USE_DENSE", "0"),
            "LEAN4_BEST_OF": os.getenv("LEAN4_BEST_OF", "8"),
        }
    )
    log.emit_json({"at": "bridge.start", "cfg": cfg})

    items = _read_inputs(args.stdin_jsonl)
    if not items:
        _emit_result({"success": False, "error": "no input"})
        return 0

    # pooled recall plan
    norm2idx = defaultdict(list)
    for i, it in enumerate(items):
        norm2idx[normalize_req(it.requirement)].append(i)
    uniq = list(norm2idx.keys())

    cache = LRU(capacity=int(os.getenv("GM_BRIDGE_LRU_SIZE", "512")))
    gm = GMRecall()
    topk = int(os.getenv("GM_BRIDGE_TOPK", "24"))
    need: Dict[str, str] = {}
    pooled: Dict[str, List[Neighbor]] = {}
    for k in uniq:
        hit = cache.get(k)
        if hit is not None:
            pooled[k] = hit
        else:
            need[k] = k
    if need and gm.available():
        log.emit_json({"at": "gm.recall_pooled.start", "n_queries": len(need)})
        res = gm.recall_pooled(need, top_k=topk)
        for k, v in res.items():
            pooled[k] = v
            cache.set(k, v)
        log.emit_json({"at": "gm.recall_pooled.done", "n_done": len(res)})
    elif need:
        log.emit_json({"lvl": "warn", "at": "gm.unavailable", "note": "fallback to lexical heuristics"})

    # fanout → lean
    timeout_s = int(os.getenv("LEAN4_TIMEOUT_SECS", "120"))
    for norm, idxs in norm2idx.items():
        neighbors = pooled.get(norm, [])
        strategies = strategies_from_neighbors(norm, neighbors)
        for idx in idxs:
            it = items[idx]
            if it.id == "" and it.requirement.startswith("<nonjson:"):
                _emit_result(
                    {
                        "id": hashlib.md5(it.requirement.encode()).hexdigest()[:8],
                        "success": False,
                        "lean_code": None,
                        "stdout": "",
                        "stderr": "non-json input line",
                        "return_code": 2,
                    }
                )
                continue

            log.emit_json({"at": "lean.run.start", "id": it.id, "best_of": len(strategies)})
            rr = run_lean(it.requirement, strategies, timeout_s=timeout_s)
            rc = int(rr.get("return_code", 1))
            success = (rc == 0) and bool(rr.get("lean_code") or rr.get("stdout"))
            if not success and "error_type" not in rr and rc != 124:
                rr["error_type"] = _classify_error(rr.get("stderr", ""), rc)
            _emit_result(
                {
                    "id": it.id,
                    "success": success,
                    "lean_code": rr.get("lean_code"),
                    "stdout": rr.get("stdout", ""),
                    "stderr": rr.get("stderr", ""),
                    "return_code": rc,
                    "tactics_used": rr.get("tactics_used"),
                    "assumptions": rr.get("assumptions"),
                }
            )
            log.emit_json({"at": "lean.run.done", "id": it.id, "success": success, "rc": rc})

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.emit_json({"lvl": "error", "at": "bridge.crash", "exc": str(exc), "trace": traceback.format_exc(limit=3)})
        _emit_result({"success": False, "error": "bridge crashed"})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
