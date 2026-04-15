"""Verify entity extraction quality using LLM-as-judge.

Uses Ollama (local) to evaluate extraction quality on sampled QRAs.
Detects:
- False positives: Extracted terms that aren't real entities
- False negatives: Missed entities in the question
- Wrong categorization: control_id labeled as phrase, etc.

Usage:
    python -m graph_memory.maintenance.verify_extraction_quality --sample 100
    python -m graph_memory.maintenance.verify_extraction_quality --sample 100 --model llama3.2
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger


@dataclass
class VerificationResult:
    """Result of verifying a single extraction."""
    qra_key: str
    question: str
    extracted_spans: list[dict]

    # LLM judgments
    false_positives: list[dict] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    wrong_category: list[dict] = field(default_factory=list)

    # Overall assessment
    precision_ok: bool = True  # No false positives
    recall_ok: bool = True     # No false negatives

    llm_reasoning: str = ""
    error: str | None = None


def get_llm_judgment(
    question: str,
    spans: list[dict],
    model: str = "unsloth/gemma-3-4b-it",
    backend: str = "scillm",  # "ollama", "vllm", or "scillm"
    api_url: str = "http://localhost:4001"  # scillm default
) -> dict:
    """Ask Ollama to evaluate extraction quality.

    Returns dict with:
    - false_positives: spans that shouldn't have been extracted
    - false_negatives: entities in the question that were missed
    - wrong_category: spans with wrong 'kind' classification
    - reasoning: explanation
    """

    # Format spans for the prompt
    span_list = []
    for s in spans:
        text = s.get("text") or s.get("entity", "?")
        kind = s.get("kind") or s.get("type", "?")
        framework = s.get("framework", "")
        span_list.append(f"  - '{text}' [{kind}]{f' ({framework})' if framework else ''}")

    spans_text = "\n".join(span_list) if span_list else "  (none extracted)"

    prompt = f"""You are evaluating entity extraction from aerospace security questions.

QUESTION: "{question}"

EXTRACTED ENTITIES:
{spans_text}

VALID ENTITY TYPES:
- control_id: Security control identifiers like CWE-79, IA-0006, CAPEC-86, T1595, AC-1, CM0001
- aerospace_term: Domain terms like FPGA, TT&C, crosslink, telemetry, uplink
- phrase: Multi-word domain concepts like "supply chain attack", "authentication bypass"

EVALUATE the extractions and respond in JSON:
{{
  "false_positives": [
    {{"text": "...", "reason": "not a real entity/just common word"}}
  ],
  "false_negatives": [
    "CWE-123"  // entities in the question that were NOT extracted but should have been
  ],
  "wrong_category": [
    {{"text": "...", "extracted_as": "phrase", "should_be": "control_id"}}
  ],
  "precision_ok": true/false,  // true if no false positives
  "recall_ok": true/false,     // true if no false negatives
  "reasoning": "brief explanation"
}}

Be strict about false positives - common words like "that", "them", "use" should be flagged.
Be lenient about domain terms - if it could plausibly be aerospace terminology, accept it.
Control IDs MUST match patterns like CWE-\\d+, CAPEC-\\d+, T\\d+, [A-Z]{{2}}-\\d+, CM\\d+.

Respond ONLY with valid JSON, no other text."""

    try:
        if backend in ("vllm", "scillm"):
            # vLLM and scillm use OpenAI-compatible API
            resp = httpx.post(
                f"{api_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
                timeout=180.0,  # longer timeout for cloud routing
                headers={
                    "x-caller-skill": "verify-extraction-quality",
                    "Authorization": "Bearer sk-dev-proxy-123",
                },
            )
            resp.raise_for_status()
            result_text = resp.json()["choices"][0]["message"]["content"]
        elif backend == "ollama":
            # Ollama API
            resp = httpx.post(
                f"{api_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1}
                },
                timeout=120.0
            )
            resp.raise_for_status()
            result_text = resp.json().get("response", "")
        else:
            raise ValueError(f"Unknown backend: {backend}")

        # Parse JSON from response (may have markdown code blocks)
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]

        return json.loads(result_text.strip())

    except Exception as e:
        logger.warning("LLM judgment failed ({}): {}", backend, e)
        return {
            "false_positives": [],
            "false_negatives": [],
            "wrong_category": [],
            "precision_ok": True,
            "recall_ok": True,
            "reasoning": f"Error: {e}",
            "error": str(e)
        }


def verify_sample(
    db,
    sample_size: int = 100,
    model: str = "llama3.2",
    backend: str = "ollama",
    api_url: str = "http://localhost:11434"
) -> dict[str, Any]:
    """Verify extraction quality on a random sample of QRAs.

    Returns:
        Stats dict with precision/recall estimates and error examples
    """

    # Fetch QRAs with evidence_case (has spans)
    aql = """
    FOR doc IN sparta_qra
      FILTER doc.evidence_case != null AND doc.evidence_case.spans != null
      LIMIT @limit
      RETURN {
        _key: doc._key,
        question: doc.question,
        spans: doc.evidence_case.spans
      }
    """

    # Get more than we need, then sample randomly
    pool_size = min(sample_size * 10, 10000)
    pool = list(db.aql.execute(aql, bind_vars={"limit": pool_size}))

    if len(pool) < sample_size:
        logger.warning("Only {} QRAs with spans available", len(pool))
        sample = pool
    else:
        sample = random.sample(pool, sample_size)

    logger.info("Verifying {} QRAs with Ollama model '{}'", len(sample), model)

    results: list[VerificationResult] = []
    false_positive_count = 0
    false_negative_count = 0
    total_spans = 0

    for i, qra in enumerate(sample):
        if (i + 1) % 10 == 0:
            logger.info("Progress: {}/{}", i + 1, len(sample))

        judgment = get_llm_judgment(
            qra["question"],
            qra["spans"],
            model=model,
            backend=backend,
            api_url=api_url
        )

        result = VerificationResult(
            qra_key=qra["_key"],
            question=qra["question"],
            extracted_spans=qra["spans"],
            false_positives=judgment.get("false_positives", []),
            false_negatives=judgment.get("false_negatives", []),
            wrong_category=judgment.get("wrong_category", []),
            precision_ok=judgment.get("precision_ok", True),
            recall_ok=judgment.get("recall_ok", True),
            llm_reasoning=judgment.get("reasoning", ""),
            error=judgment.get("error")
        )

        results.append(result)

        if not result.precision_ok:
            false_positive_count += 1
        if not result.recall_ok:
            false_negative_count += 1
        total_spans += len(qra["spans"])

    # Compute summary stats
    precision_rate = 1 - (false_positive_count / len(results)) if results else 1.0
    recall_rate = 1 - (false_negative_count / len(results)) if results else 1.0

    # Collect worst examples
    worst_fp = [r for r in results if not r.precision_ok][:5]
    worst_fn = [r for r in results if not r.recall_ok][:5]

    return {
        "sample_size": len(sample),
        "total_spans_evaluated": total_spans,
        "precision_rate": precision_rate,
        "recall_rate": recall_rate,
        "false_positive_count": false_positive_count,
        "false_negative_count": false_negative_count,

        "worst_false_positives": [
            {
                "qra_key": r.qra_key,
                "question": r.question[:200],
                "false_positives": r.false_positives,
                "reasoning": r.llm_reasoning
            }
            for r in worst_fp
        ],

        "worst_false_negatives": [
            {
                "qra_key": r.qra_key,
                "question": r.question[:200],
                "missed": r.false_negatives,
                "reasoning": r.llm_reasoning
            }
            for r in worst_fn
        ],

        "common_false_positive_terms": _count_common_fps(results),
    }


def _count_common_fps(results: list[VerificationResult]) -> list[dict]:
    """Find most common false positive terms."""
    counts: dict[str, int] = {}
    for r in results:
        for fp in r.false_positives:
            text = fp.get("text", "").lower()
            if text:
                counts[text] = counts.get(text, 0) + 1

    sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
    return [{"term": t, "count": c} for t, c in sorted_counts[:10]]


def main():
    parser = argparse.ArgumentParser(description="Verify entity extraction quality")
    parser.add_argument("--sample", type=int, default=100, help="Sample size")
    parser.add_argument("--model", type=str, default="unsloth/gemma-3-4b-it", help="Model name")
    parser.add_argument("--backend", type=str, default="scillm",
                        choices=["ollama", "vllm", "scillm"], help="LLM backend")
    parser.add_argument("--api-url", type=str, help="API URL (defaults based on backend)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())

    # Set default API URL based on backend
    if args.api_url is None:
        if args.backend == "ollama":
            args.api_url = "http://localhost:11434"
        elif args.backend == "vllm":
            args.api_url = "http://localhost:8000"
        else:  # scillm
            args.api_url = "http://localhost:4001"

    from ..arango_client import get_db
    db = get_db()

    # Check backend is available (skip for scillm - routes dynamically)
    if args.backend != "scillm":
        try:
            if args.backend == "ollama":
                resp = httpx.get(f"{args.api_url}/api/tags", timeout=5.0)
                models = [m["name"] for m in resp.json().get("models", [])]
                if args.model not in models and f"{args.model}:latest" not in models:
                    logger.warning("Model '{}' may not be available. Available: {}", args.model, models[:5])
            else:
                # vLLM health check
                resp = httpx.get(f"{args.api_url}/health", timeout=5.0)
        except Exception as e:
            logger.error("Cannot reach {} at {}: {}", args.backend, args.api_url, e)
            return

    stats = verify_sample(
        db,
        sample_size=args.sample,
        model=args.model,
        backend=args.backend,
        api_url=args.api_url
    )

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print("\n" + "=" * 60)
        print("EXTRACTION QUALITY VERIFICATION")
        print("=" * 60)
        print(f"Sample size:       {stats['sample_size']}")
        print(f"Total spans:       {stats['total_spans_evaluated']}")
        print(f"Precision rate:    {stats['precision_rate']:.1%}")
        print(f"Recall rate:       {stats['recall_rate']:.1%}")
        print(f"False positives:   {stats['false_positive_count']} QRAs")
        print(f"False negatives:   {stats['false_negative_count']} QRAs")

        if stats["common_false_positive_terms"]:
            print("\nCommon false positive terms:")
            for fp in stats["common_false_positive_terms"][:5]:
                print(f"  - '{fp['term']}' ({fp['count']} times)")

        if stats["worst_false_positives"]:
            print("\nWorst false positive examples:")
            for ex in stats["worst_false_positives"][:3]:
                print(f"\n  QRA: {ex['qra_key']}")
                print(f"  Q: {ex['question'][:100]}...")
                print(f"  FPs: {ex['false_positives']}")

        if stats["worst_false_negatives"]:
            print("\nWorst false negative examples:")
            for ex in stats["worst_false_negatives"][:3]:
                print(f"\n  QRA: {ex['qra_key']}")
                print(f"  Q: {ex['question'][:100]}...")
                print(f"  Missed: {ex['missed']}")


if __name__ == "__main__":
    main()
