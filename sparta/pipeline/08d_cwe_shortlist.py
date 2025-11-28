#!/usr/bin/env python3
"""
Step 08d – Deterministic CWE shortlist per control (cheap prefilter).

Reads:
  - controls_context.jsonl (control_id, title, definition, category)
  - cwe_summaries.jsonl (from 08c; cwe_id, title, summary, candidate_categories?)

Outputs:
  sparta/data/processed/cwe_shortlist.jsonl with rows:
    {
      "control_id": "...",
      "control_title": "...",
      "category": "...",
      "candidates": [
         {"cwe_id": "...", "score": 0.42, "title": "...", "summary": "..."},
         ...
      ]
    }

Scoring: simple token-overlap score between control text (title + definition)
and CWE (title + summary). This is a *soft* filter to reduce the set sent to
LLM adjudication. It is not a gate; the LLM still decides final relevance.

Usage example:
  set -a && source .env && set +a && \
  python sparta/pipeline/08d_cwe_shortlist.py \
    --controls sparta/data/processed/controls_context.jsonl \
    --cwes sparta/data/processed/cwe_summaries.jsonl \
    --out sparta/data/processed/cwe_shortlist.jsonl \
    --topk 50 \
    --category Prevention
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Dict, Any, List, Iterable, Tuple


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _tokens(text: str) -> List[str]:
    # lowercase, split on non-letters/digits, drop very short tokens
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2]


def _score_overlap(ctrl_tokens: List[str], cwe_tokens: List[str]) -> float:
    if not ctrl_tokens or not cwe_tokens:
        return 0.0
    ctrl_set = set(ctrl_tokens)
    cwe_set = set(cwe_tokens)
    inter = len(ctrl_set & cwe_set)
    denom = math.sqrt(len(ctrl_set) * len(cwe_set))
    return inter / denom if denom else 0.0


def build_shortlist(
    controls_path: Path,
    cwes_path: Path,
    out_path: Path,
    topk: int,
    category_filter: str | None = None,
) -> Dict[str, Any]:
    cwes = list(_read_jsonl(cwes_path))
    if not cwes:
        return {"ok": False, "error": "no_cwes", "path": str(cwes_path)}

    # Pre-tokenize CWEs
    cwe_cache: Dict[str, Tuple[str, str, List[str]]] = {}
    for row in cwes:
        cid = row.get("cwe_id")
        if not cid:
            continue
        text = " ".join(
            [
                str(row.get("title") or ""),
                str(row.get("summary") or ""),
            ]
        )
        cwe_cache[cid] = (
            str(row.get("title") or ""),
            str(row.get("summary") or ""),
            _tokens(text),
        )

    written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for ctrl in _read_jsonl(controls_path):
            cat = ctrl.get("category")
            if category_filter and cat and cat.lower() != category_filter.lower():
                continue
            ctrl_id = ctrl.get("id") or ctrl.get("control_id")
            title = ctrl.get("title") or ""
            definition = ctrl.get("definition") or ""
            ctrl_tokens = _tokens(f"{title} {definition}")
            if not ctrl_tokens:
                continue

            scores: List[Tuple[float, str]] = []
            for cid, (_t, _s, tok) in cwe_cache.items():
                score = _score_overlap(ctrl_tokens, tok)
                if score > 0:
                    scores.append((score, cid))

            scores.sort(reverse=True, key=lambda x: x[0])
            picked = scores[:topk]

            if not picked:
                continue

            candidates: List[Dict[str, Any]] = []
            for score, cid in picked:
                title_cwe, summary_cwe, _ = cwe_cache[cid]
                candidates.append(
                    {
                        "cwe_id": cid,
                        "score": round(score, 4),
                        "title": title_cwe,
                        "summary": summary_cwe,
                    }
                )

            fh.write(
                json.dumps(
                    {
                        "control_id": ctrl_id,
                        "control_title": title,
                        "category": cat,
                        "candidates": candidates,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1

    return {"ok": True, "written": written, "out": str(out_path), "topk": topk}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build deterministic CWE shortlist per control.")
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--controls", type=Path, default=root / "data" / "processed" / "controls_context.jsonl")
    parser.add_argument("--cwes", type=Path, default=root / "data" / "processed" / "cwe_summaries.jsonl")
    parser.add_argument("--out", type=Path, default=root / "data" / "processed" / "cwe_shortlist.jsonl")
    parser.add_argument("--topk", type=int, default=50)
    parser.add_argument("--category", type=str, default=None, help="Optional category filter (e.g., Prevention)")
    args = parser.parse_args(argv)

    res = build_shortlist(
        controls_path=args.controls,
        cwes_path=args.cwes,
        out_path=args.out,
        topk=args.topk,
        category_filter=args.category,
    )
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
