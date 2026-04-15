"""Evaluate a trained reranker/embedding bundle and gate promotion.

If SEAL_EVAL_BIN (or default ../devops/sparta/pipeline/seal_eval.sh) exists and is
executable, this script will call it and expect a JSON with `mrr` and `ndcg`.
Gates: MRR >= SEAL_GATE_MRR_MIN (default 0.25) and NDCG >= SEAL_GATE_NDCG_MIN (default 0.25).
If no evaluator is present, we fall back to a bundle-exists check (best-effort).
"""
from __future__ import annotations
import json
from pathlib import Path
import os
import subprocess
import typer

app = typer.Typer(add_completion=False)

@app.command()
def main(
    bundle: Path = typer.Option(..., exists=True, dir_okay=True, file_okay=True),
    scope: str = typer.Option("", help="Scope for downstream use"),
    json_out: bool = typer.Option(True, "--json"),
    prefer_best_epoch: bool = typer.Option(True, "--prefer-best-epoch", help="If evaluator returns best checkpoints, pick best by metric"),
):
    ok = bundle.exists()
    errors = [] if ok else ["bundle missing"]
    gate_ok = ok
    eval_meta = {}

    eval_bin = Path(os.getenv("SEAL_EVAL_BIN", "../devops/sparta/pipeline/seal_eval.sh"))
    gate_mrr = float(os.getenv("SEAL_GATE_MRR_MIN", "0.25") or 0.25)
    gate_ndcg = float(os.getenv("SEAL_GATE_NDCG_MIN", "0.25") or 0.25)

    if ok and eval_bin.exists() and os.access(eval_bin, os.X_OK):
        try:
            proc = subprocess.run(
                [str(eval_bin), str(bundle), scope or ""],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                errors.append(f"evaluator failed: {proc.stderr.strip()}")
                gate_ok = False
            else:
                js = json.loads(proc.stdout.strip() or "{}")
                # If evaluator returns list of checkpoints, pick best by mrr/ndcg
                if prefer_best_epoch and isinstance(js.get("checkpoints"), list):
                    best = None
                    for ck in js["checkpoints"]:
                        mrr_ck = float(ck.get("mrr", 0.0))
                        ndcg_ck = float(ck.get("ndcg", 0.0))
                        if best is None or (mrr_ck, ndcg_ck) > (best.get("mrr", 0.0), best.get("ndcg", 0.0)):
                            best = ck
                    if best:
                        eval_meta = {"mrr": best.get("mrr", 0.0), "ndcg": best.get("ndcg", 0.0), "bundle": best.get("bundle") or str(bundle)}
                        bundle = Path(best.get("bundle") or bundle)
                        if eval_meta["mrr"] < gate_mrr or eval_meta["ndcg"] < gate_ndcg:
                            gate_ok = False
                            errors.append(f"gate fail: mrr {eval_meta['mrr']:.3f} < {gate_mrr} or ndcg {eval_meta['ndcg']:.3f} < {gate_ndcg}")
                else:
                    mrr = float(js.get("mrr", 0.0))
                    ndcg = float(js.get("ndcg", 0.0))
                    eval_meta = {"mrr": mrr, "ndcg": ndcg}
                    if mrr < gate_mrr or ndcg < gate_ndcg:
                        gate_ok = False
                        errors.append(f"gate fail: mrr {mrr:.3f} < {gate_mrr} or ndcg {ndcg:.3f} < {gate_ndcg}")
        except Exception as e:
            errors.append(f"eval error: {e}")
            gate_ok = False

    res = {
        "meta": {"ok": gate_ok, "bundle": str(bundle), "scope": scope, "eval": eval_meta},
        "items": [],
        "errors": errors,
    }
    if json_out:
        print(json.dumps(res))
    else:
        print(res)

if __name__ == "__main__":
    app()
