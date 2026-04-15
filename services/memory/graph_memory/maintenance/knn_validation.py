"""KNN Validation Experiment.

Tests whether KNN similarity scores predict LLM edge verification outcomes.

Usage:
    # Sample and verify (dry run)
    uv run python -m graph_memory.maintenance.knn_validation sample --dry-run

    # Run experiment (uses LLM)
    uv run python -m graph_memory.maintenance.knn_validation sample --per-stratum 30

    # Analyze results
    uv run python -m graph_memory.maintenance.knn_validation analyze
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from arango import ArangoClient
from rich.console import Console
from rich.table import Table

app = typer.Typer()
console = Console()

# Strata definitions
STRATA = {
    "low": (0.0, 0.4),
    "med": (0.4, 0.7),
    "high": (0.7, 1.0),
}


def get_db():
    """Get ArangoDB connection."""
    client = ArangoClient(hosts=f"http://{os.getenv('ARANGO_HOST', 'localhost')}:{os.getenv('ARANGO_PORT', '8529')}")
    return client.db(
        os.getenv("ARANGO_DB", "memory"),
        username=os.getenv("ARANGO_USER", "root"),
        password=os.getenv("ARANGO_PASS", ""),
    )


def get_unverified_by_stratum(db, per_stratum: int = 30) -> Dict[str, List[dict]]:
    """Get stratified sample of unverified edges."""
    samples = {}

    for name, (low, high) in STRATA.items():
        edges = list(db.aql.execute("""
            FOR e IN lesson_edges
            FILTER e.llm_confidence == null
            FILTER e.raw_sim >= @low AND e.raw_sim < @high
            RETURN {
                _key: e._key,
                _from: e._from,
                _to: e._to,
                raw_sim: e.raw_sim,
                rationale: e.rationale
            }
        """, bind_vars={"low": low, "high": high}))

        # Random sample
        if len(edges) > per_stratum:
            samples[name] = random.sample(edges, per_stratum)
        else:
            samples[name] = edges

        console.print(f"[cyan]{name.upper()}[/cyan] ({low}-{high}): {len(edges)} available, sampled {len(samples[name])}")

    return samples


def build_verification_prompt(from_doc: dict, to_doc: dict) -> str:
    """Build the edge verification prompt."""
    return f"""Rate the semantic relationship between these two knowledge items on a scale of 0-1.

ITEM A:
Title: {from_doc.get('title', 'N/A')}
Problem: {from_doc.get('problem', 'N/A')[:500]}

ITEM B:
Title: {to_doc.get('title', 'N/A')}
Problem: {to_doc.get('problem', 'N/A')[:500]}

Respond with JSON only: {{"confidence": 0.X, "rationale": "brief reason"}}

Scoring guide:
- 0.0-0.3: Unrelated topics, no meaningful connection
- 0.4-0.6: Tangentially related, share some context but different focus
- 0.7-0.8: Related, address similar problems or share concepts
- 0.9-1.0: Strongly related or near-duplicates

Be CRITICAL - only score > 0.5 if there's clear topical overlap."""


async def verify_edges_batch_scillm(db, edges: List[dict]) -> List[dict]:
    """Verify edges using scillm HTTP API batch completions."""
    import asyncio
    import httpx

    # Build prompts for all edges
    prompts = []
    edge_docs = []

    for edge in edges:
        from_doc = db.collection("lessons").get(edge["_from"].split("/")[1])
        to_doc = db.collection("lessons").get(edge["_to"].split("/")[1])

        if not from_doc or not to_doc:
            edge_docs.append(None)
            prompts.append(None)
        else:
            edge_docs.append((from_doc, to_doc))
            prompts.append(build_verification_prompt(from_doc, to_doc))

    # Filter valid prompts
    valid_indices = [i for i, p in enumerate(prompts) if p is not None]
    valid_prompts = [prompts[i] for i in valid_indices]

    if not valid_prompts:
        return [{"llm_confidence": None, "error": "no valid edges"} for _ in edges]

    # scillm is HTTP API at localhost:4001
    api_base = os.getenv("SCILLM_API_BASE", "http://localhost:4001")
    api_key = os.getenv("SCILLM_PROXY_KEY", "sk-dev-proxy-123")
    model = os.getenv("SCILLM_DEFAULT_MODEL", "text")

    results = [{"llm_confidence": None, "error": "not processed"} for _ in edges]

    console.print(f"[cyan]Running {len(valid_prompts)} prompts via scillm...[/cyan]")

    async def call_one(prompt: str, orig_idx: int) -> tuple[int, dict]:
        """Call scillm for a single prompt."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    f"{api_base}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "x-expect-json": "true",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
                parsed = json.loads(content)
                return orig_idx, {
                    "llm_confidence": parsed.get("confidence", 0.5),
                    "llm_rationale": parsed.get("rationale", ""),
                }
            except httpx.HTTPStatusError as e:
                return orig_idx, {"llm_confidence": None, "error": f"HTTP {e.response.status_code}"}
            except json.JSONDecodeError as e:
                return orig_idx, {"llm_confidence": None, "error": f"JSON parse: {e}"}
            except Exception as e:
                return orig_idx, {"llm_confidence": None, "error": str(e)}

    # Run with bounded concurrency
    semaphore = asyncio.Semaphore(6)

    async def bounded_call(prompt: str, idx: int) -> tuple[int, dict]:
        async with semaphore:
            return await call_one(prompt, idx)

    tasks = [bounded_call(p, valid_indices[i]) for i, p in enumerate(valid_prompts)]
    for coro in asyncio.as_completed(tasks):
        orig_idx, result = await coro
        results[orig_idx] = result

    return results


def verify_edge_llm(db, edge: dict, dry_run: bool = False) -> dict:
    """Verify a single edge with LLM (sync wrapper)."""
    if dry_run:
        return {"llm_confidence": None, "dry_run": True}

    # Get lesson contents
    from_doc = db.collection("lessons").get(edge["_from"].split("/")[1])
    to_doc = db.collection("lessons").get(edge["_to"].split("/")[1])

    if not from_doc or not to_doc:
        return {"llm_confidence": None, "error": "lesson not found"}

    # ALL LLM calls go through scillm — no litellm/openai bypass
    from graph_memory.llm.provider import completion as _scillm_completion

    prompt = build_verification_prompt(from_doc, to_doc)

    model = "text"  # scillm handles routing
    txt, _dur, _prov = _scillm_completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
    )
    content = txt
    if not content:
        return {"llm_confidence": None, "error": "scillm returned no response"}
    try:
        data = json.loads(content)
        return {
            "llm_confidence": data.get("confidence", 0.5),
            "llm_rationale": data.get("rationale", ""),
        }
    except json.JSONDecodeError:
        return {"llm_confidence": None, "error": "invalid json"}


@app.command()
def sample(
    per_stratum: int = typer.Option(80, help="Samples per stratum for statistical power (default 80)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't call LLM"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output JSON file"),
    use_scillm: bool = typer.Option(True, "--scillm/--no-scillm", help="Use scillm batch (recommended)"),
):
    """Stratified sample of unverified edges, verified via scillm.

    Statistical design:
    - Stratified by KNN similarity (LOW/MED/HIGH) to ensure representation
    - 80 samples per stratum = 240 total for 80% power to detect medium effect
    - LOW stratum may have fewer if < 80 edges available (all will be sampled)

    Budget: ~240 calls = 4.8% of 5000/day Chutes budget
    """
    import asyncio

    db = get_db()

    total_calls = per_stratum * len(STRATA)
    console.print("\n[bold]KNN Validation Experiment[/bold]")
    console.print(f"Sampling {per_stratum} edges per stratum")
    console.print(f"[yellow]Budget: ~{total_calls} LLM calls ({total_calls/50:.1f}% of 5000/day)[/yellow]\n")

    samples = get_unverified_by_stratum(db, per_stratum)

    results = {"timestamp": datetime.now().isoformat(), "strata": {}, "budget_used": 0}

    if use_scillm and not dry_run:
        # Batch all edges together for efficiency
        all_edges = []
        edge_strata = []
        for stratum, edges in samples.items():
            for edge in edges:
                all_edges.append(edge)
                edge_strata.append(stratum)

        console.print(f"\n[bold cyan]Verifying {len(all_edges)} edges via scillm batch...[/bold cyan]")

        # Run batch verification
        batch_results = asyncio.run(verify_edges_batch_scillm(db, all_edges))
        results["budget_used"] = len(all_edges)

        # Distribute results back to strata
        for stratum in STRATA.keys():
            results["strata"][stratum] = []

        for i, (edge, result) in enumerate(zip(all_edges, batch_results)):
            stratum = edge_strata[i]
            results["strata"][stratum].append({
                "edge_key": edge["_key"],
                "raw_sim": edge["raw_sim"],
                **result,
            })
    else:
        # Sequential processing (dry run or fallback)
        for stratum, edges in samples.items():
            console.print(f"\n[bold cyan]Verifying {stratum.upper()} stratum ({len(edges)} edges)...[/bold cyan]")

            stratum_results = []
            for i, edge in enumerate(edges):
                if not dry_run:
                    console.print(f"  [{i+1}/{len(edges)}] {edge['_key'][:8]}... raw_sim={edge['raw_sim']:.3f}", end=" ")

                result = verify_edge_llm(db, edge, dry_run)
                results["budget_used"] += 0 if dry_run else 1

                stratum_results.append({
                    "edge_key": edge["_key"],
                    "raw_sim": edge["raw_sim"],
                    **result,
                })

                if not dry_run and result.get("llm_confidence") is not None:
                    console.print(f"→ llm={result['llm_confidence']:.2f}")

            results["strata"][stratum] = stratum_results

    # Save results
    if output:
        out_path = Path(output)
    else:
        out_path = Path.home() / ".local" / "state" / "memory" / f"knn_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    console.print(f"\n[green]Results saved to {out_path}[/green]")

    # Quick analysis
    if not dry_run:
        analyze_results(results)


def analyze_results(results: dict):
    """Analyze validation results with statistical tests."""
    console.print("\n[bold]Analysis[/bold]")

    table = Table(title="KNN vs LLM Correlation by Stratum")
    table.add_column("Stratum")
    table.add_column("n")
    table.add_column("Avg KNN")
    table.add_column("Avg LLM")
    table.add_column("Std LLM")
    table.add_column("Rejected (<0.5)")
    table.add_column("Corr")

    all_knn = []
    all_llm = []
    stratum_data = {}

    for stratum, items in results.get("strata", {}).items():
        valid = [i for i in items if i.get("llm_confidence") is not None]
        if not valid:
            continue

        knn_scores = [i["raw_sim"] for i in valid]
        llm_scores = [i["llm_confidence"] for i in valid]

        all_knn.extend(knn_scores)
        all_llm.extend(llm_scores)
        stratum_data[stratum] = {"knn": knn_scores, "llm": llm_scores}

        avg_knn = sum(knn_scores) / len(knn_scores)
        avg_llm = sum(llm_scores) / len(llm_scores)
        std_llm = (sum((x - avg_llm)**2 for x in llm_scores) / len(llm_scores)) ** 0.5
        rejected = sum(1 for s in llm_scores if s < 0.5)

        # Per-stratum correlation
        corr = _pearson(knn_scores, llm_scores) if len(valid) > 2 else None

        table.add_row(
            stratum.upper(),
            str(len(valid)),
            f"{avg_knn:.3f}",
            f"{avg_llm:.3f}",
            f"{std_llm:.3f}",
            f"{rejected} ({100*rejected/len(valid):.0f}%)",
            f"{corr:.3f}" if corr else "N/A",
        )

    console.print(table)

    # Overall correlation
    if len(all_knn) > 10:
        overall_corr = _pearson(all_knn, all_llm)
        console.print(f"\n[bold]Overall Pearson correlation (r):[/bold] {overall_corr:.3f}")

        # Interpret effect size
        if abs(overall_corr) > 0.5:
            effect = "STRONG"
            color = "green"
        elif abs(overall_corr) > 0.3:
            effect = "MODERATE"
            color = "yellow"
        elif abs(overall_corr) > 0.1:
            effect = "WEAK"
            color = "yellow"
        else:
            effect = "NEGLIGIBLE"
            color = "red"

        console.print(f"[{color}]Effect size: {effect}[/{color}]")

    # Statistical significance test: Compare rejection rates across strata
    console.print("\n[bold]Hypothesis Test: Do rejection rates differ by KNN stratum?[/bold]")

    if len(stratum_data) >= 2:
        # Chi-squared test for independence
        rejection_counts = {}
        for name, data in stratum_data.items():
            n = len(data["llm"])
            rejected = sum(1 for x in data["llm"] if x < 0.5)
            rejection_counts[name] = {"rejected": rejected, "accepted": n - rejected, "n": n}

        console.print("\nContingency table:")
        for name, counts in rejection_counts.items():
            pct = 100 * counts["rejected"] / counts["n"] if counts["n"] > 0 else 0
            console.print(f"  {name.upper()}: {counts['rejected']}/{counts['n']} rejected ({pct:.1f}%)")

        # Simple chi-squared approximation
        total_n = sum(c["n"] for c in rejection_counts.values())
        total_rejected = sum(c["rejected"] for c in rejection_counts.values())
        expected_rate = total_rejected / total_n if total_n > 0 else 0

        chi2 = 0
        for name, counts in rejection_counts.items():
            if counts["n"] > 0:
                expected_rejected = expected_rate * counts["n"]
                expected_accepted = (1 - expected_rate) * counts["n"]
                if expected_rejected > 0:
                    chi2 += (counts["rejected"] - expected_rejected)**2 / expected_rejected
                if expected_accepted > 0:
                    chi2 += (counts["accepted"] - expected_accepted)**2 / expected_accepted

        df = len(rejection_counts) - 1
        # Critical values: df=2, α=0.05 -> 5.99; α=0.01 -> 9.21
        console.print(f"\nChi-squared statistic: {chi2:.2f} (df={df})")
        if df == 2:
            if chi2 > 9.21:
                console.print("[green]p < 0.01: Rejection rates SIGNIFICANTLY differ by KNN stratum[/green]")
            elif chi2 > 5.99:
                console.print("[yellow]p < 0.05: Rejection rates significantly differ by KNN stratum[/yellow]")
            else:
                console.print("[red]p > 0.05: No significant difference in rejection rates[/red]")

    # Recommendation
    console.print("\n[bold]Recommendation:[/bold]")
    if len(all_knn) > 10:
        if overall_corr > 0.3:
            console.print("[green]KNN is predictive. Strategy:[/green]")
            console.print("  - HIGH KNN (>0.7): Auto-approve, skip LLM verification")
            console.print("  - MED KNN (0.4-0.7): LLM verify (prioritize these)")
            console.print("  - LOW KNN (<0.4): Auto-reject or LLM verify")
        elif overall_corr > 0.1:
            console.print("[yellow]KNN has weak signal. Strategy:[/yellow]")
            console.print("  - Use KNN for prioritization only, not auto-decisions")
            console.print("  - LLM verify all edges, but process HIGH KNN last")
        else:
            console.print("[red]KNN is not predictive. Strategy:[/red]")
            console.print("  - Do not use KNN for filtering")
            console.print("  - Consider alternative features (tag overlap, scope, etc.)")
            console.print("  - LLM verify all edges")


def _pearson(x: List[float], y: List[float]) -> float:
    """Calculate Pearson correlation coefficient."""
    n = len(x)
    if n < 2:
        return 0.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y)) / n
    std_x = (sum((xi - mean_x)**2 for xi in x) / n) ** 0.5
    std_y = (sum((yi - mean_y)**2 for yi in y) / n) ** 0.5

    if std_x == 0 or std_y == 0:
        return 0.0

    return cov / (std_x * std_y)


@app.command()
def analyze(
    input_file: Optional[str] = typer.Option(None, "--input", "-i", help="Results JSON file"),
):
    """Analyze existing validation results."""
    if input_file:
        path = Path(input_file)
    else:
        # Find most recent
        state_dir = Path.home() / ".local" / "state" / "memory"
        files = sorted(state_dir.glob("knn_validation_*.json"), reverse=True)
        if not files:
            console.print("[red]No validation results found[/red]")
            raise typer.Exit(1)
        path = files[0]

    console.print(f"Analyzing: {path}")

    with open(path) as f:
        results = json.load(f)

    analyze_results(results)


@app.command()
def status():
    """Show current edge verification status."""
    db = get_db()

    table = Table(title="Edge Verification Status")
    table.add_column("Stratum")
    table.add_column("Total")
    table.add_column("Verified")
    table.add_column("Unverified")
    table.add_column("Avg LLM (verified)")

    for name, (low, high) in STRATA.items():
        stats = list(db.aql.execute("""
            FOR e IN lesson_edges
            FILTER e.raw_sim >= @low AND e.raw_sim < @high
            COLLECT verified = (e.llm_confidence != null)
            AGGREGATE cnt = COUNT(1), avg_llm = AVG(e.llm_confidence)
            RETURN {verified, cnt, avg_llm}
        """, bind_vars={"low": low, "high": high}))

        verified = next((s for s in stats if s["verified"]), {"cnt": 0, "avg_llm": None})
        unverified = next((s for s in stats if not s["verified"]), {"cnt": 0})

        table.add_row(
            f"{name.upper()} ({low}-{high})",
            str(verified["cnt"] + unverified["cnt"]),
            str(verified["cnt"]),
            str(unverified["cnt"]),
            f"{verified['avg_llm']:.3f}" if verified["avg_llm"] else "N/A",
        )

    console.print(table)


if __name__ == "__main__":
    app()
