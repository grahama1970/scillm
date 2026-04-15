from __future__ import annotations
import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Optional, List
from loguru import logger

MATH_TOKENS = (
    r"\bprove\b", r"\bshow that\b", r"\blemma\b", r"\btheorem\b",
    r"∀", r"∃", r"→", r"≤", r"≥", r"=", r"≠", r"∑", r"∏", r"\bnat\b", r"\bfinset\b",
)
MATH_RE = re.compile("|".join(MATH_TOKENS), re.I)
VAR_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_]*)\b")
NUM_RE = re.compile(r"\b\d+\b")

# Optional Graph Memory recall (Arango-backed)
try:  # late import; keep this module usable without full GM
    from graph_memory.lessons import recall as gm_recall  # type: ignore
except Exception as exc:
    logger.error("Suppressed error in formalize_gate: {}", exc)
    gm_recall = None


@dataclass
class FormalizeDecision:
    action: str  # one of: FORMALIZE, CLARIFY, SKIP
    reason: str
    s_formal: float
    margin: float
    entropy: float
    clarifying_question: Optional[str] = None
    s_prior: float = 0.0


def score_formalizability(q: str) -> float:
    """Heuristic s_formal in [0,1] from token features.
    Weights are light, interpretable, and easy to tune.
    """
    s = 0.0
    ql = q.lower()
    # strong cues
    if any(tok in ql for tok in ("prove", "show that", "lemma", "theorem")):
        s += 0.4
    if any(sym in q for sym in ("∀", "∃", "∑", "∏")):
        s += 0.3
    if any(sym in q for sym in ("≤", "≥", "=", "→")):
        s += 0.2
    if any(tok in ql for tok in ("finset", "nat", ":", " ∈ ", " in ")):
        s += 0.1
    return max(0.0, min(1.0, s))


def _prior_formal_signal(query: str, scope: Optional[str], k: int = 12) -> float:
    """Look at recent/nearby lessons for formal cues to bias the decision.
    Returns fraction in [0,1] of neighbors that look formal (have math tokens or a stored lean_goal).
    """
    if gm_recall is None or not scope:
        return 0.0
    try:
        res = gm_recall.recall(query=query, scope=scope, top_k=k) or []
    except Exception as exc:
        logger.error("Suppressed error in formalize_gate: {}", exc)
        return 0.0
    if not res:
        return 0.0
    formal = 0
    for r in res:
        title = (r.get("title") or "")
        text = (r.get("text") or "")
        tags = " ".join(r.get("tags") or [])
        blob = f"{title}\n{text}\n{tags}"
        if MATH_RE.search(blob):
            formal += 1
        elif "lean_goal" in r and r.get("lean_goal"):
            formal += 1
    return formal / max(1, len(res))


def compute_margin_entropy(scores: List[float]) -> tuple[float, float]:
    if not scores:
        return 1.0, 0.0
    xs = sorted(scores, reverse=True)
    s1 = xs[0]
    s5 = xs[4] if len(xs) >= 5 else xs[-1]
    margin = float(s1 - s5)
    # softmax then Shannon entropy
    import math
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    Z = sum(exps) or 1.0
    p = [e / Z for e in exps]
    entropy = -sum(pi * math.log(max(pi, 1e-12)) for pi in p)
    return margin, entropy


def should_formalize(
    q: str,
    *,
    fused_margin: float = 1.0,
    category: Optional[str] = None,
    cats: Optional[list[str]] = None,
    # Align function defaults with CLI defaults (env‑tunable):
    margin_threshold: float = float(os.getenv("CLARIFY_MARGIN_MIN", 0.18)),
    entropy_threshold: float = float(os.getenv("FORMAL_ENTROPY_MIN", 1.7)),
    topk_scores: Optional[List[float]] = None,
    scope: Optional[str] = None,
    prior_k: int = 12,
) -> FormalizeDecision:
    """Decide whether to formalize a question, ask for clarification, or skip.

    Policy:
      - If category is within formal cats or math tokens present, consider formalization.
      - If fused margin is low (ambiguous), prefer formalization.
      - If we detect untyped variables/bounds likely needed, ask for one clarifying turn.
    """
    q_norm = q.strip()
    cats = cats or [c.strip() for c in os.getenv("FORMALIZE_CATS", "math,logic,proof").split(",") if c.strip()]
    is_formal_cat = (category or "").lower() in {c.lower() for c in cats}
    has_math_tokens = bool(MATH_RE.search(q_norm))
    s_formal = score_formalizability(q_norm)
    s_prior = _prior_formal_signal(q_norm, scope=scope, k=int(prior_k)) if scope else 0.0
    # boost by prior formal context (bounded)
    s_formal = min(1.0, s_formal + 0.5 * s_prior)
    # optionally update margin/entropy from top-k scores if provided
    if topk_scores:
        fused_margin, entropy = compute_margin_entropy(topk_scores)
    else:
        # entropy unknown; set to 0 so it never triggers on its own
        entropy = 0.0
    low_margin = fused_margin < margin_threshold or entropy > entropy_threshold

    if not (is_formal_cat or has_math_tokens or s_formal >= 0.6 or (low_margin and s_formal >= 0.4)):
        return FormalizeDecision("SKIP", reason="Not a formal category and no strong math cues; high margin", s_formal=s_formal, margin=fused_margin, entropy=entropy, s_prior=s_prior)

    # Heuristic: extract candidate variables (exclude obvious words) and check if bounds/types are present
    vars_ = set(VAR_RE.findall(q_norm)) - {"prove", "show", "that", "the", "a", "an", "forall", "exists", "sum", "range"}
    nums_ = bool(NUM_RE.search(q_norm))
    needs_types = bool(vars_) and not any(tok in q_norm for tok in (":", "∈", "in ", " : ", " :="))
    needs_bounds = ("range" in q_norm.lower() or "sum" in q_norm.lower() or "∑" in q_norm) and not any(b in q_norm for b in ("..", "< n", "≤ n", "range"))

    if needs_types or needs_bounds:
        ex = "n: ℕ, i ∈ Finset.range n" if ("sum" in q_norm.lower() or "∑" in q_norm) else "x: ℝ"
        cq = f"To formalize this as a Lean goal, can you confirm variable types/bounds? For example: {ex}."
        return FormalizeDecision("CLARIFY", reason="Missing types/bounds", clarifying_question=cq, s_formal=s_formal, margin=fused_margin, entropy=entropy, s_prior=s_prior)
    return FormalizeDecision("FORMALIZE", reason="Formal category/cues or low margin", s_formal=s_formal, margin=fused_margin, entropy=entropy, s_prior=s_prior)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--fused-margin", type=float, default=1.0)
    ap.add_argument("--cat", default="")
    ap.add_argument("--scope", default="", help="GM scope for prior context bias")
    ap.add_argument("--margin-threshold", type=float, default=float(os.getenv("CLARIFY_MARGIN_MIN", 0.18)))
    ap.add_argument("--entropy-threshold", type=float, default=float(os.getenv("FORMAL_ENTROPY_MIN", 1.7)))
    ap.add_argument("--scores", nargs='*', type=float, default=None, help="Optional top-k fused scores to compute margin/entropy")
    ap.add_argument("--prior-k", type=int, default=12, help="Neighbors to inspect for prior formal signal")
    args = ap.parse_args()
    dec = should_formalize(
        args.q,
        fused_margin=args.fused_margin,
        category=args.cat,
        margin_threshold=args.margin_threshold,
        entropy_threshold=args.entropy_threshold,
        topk_scores=args.scores,
        scope=(args.scope or None),
        prior_k=int(args.prior_k),
    )
    print(json.dumps(dec.__dict__, ensure_ascii=False))


if __name__ == "__main__":
    main()
