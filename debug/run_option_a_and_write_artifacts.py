from __future__ import annotations
import os, json, pathlib, csv, sys
from scillm.extras.multi_agents import answer_code_autogen_and_judge_codex_only

def main() -> None:
    os.environ.setdefault("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089")
    items = [{"task": "Six improved fast inverse square root variants for a gaming plugin (C/C++).", "context": {}}]
    res = answer_code_autogen_and_judge_codex_only(
        items,
        n_variants=6,
        generator_model="gpt-5",
        temperature=0.0,
        max_tokens=2000,
        judge_model="codex-agent/gpt-5",
        timeout=90.0,
    )
    outdir = pathlib.Path('.artifacts/option_a')
    outdir.mkdir(parents=True, exist_ok=True)
    # Write winners.jsonl (single line with best id + rationale)
    judge = res.get("judge") or {}
    with (outdir / 'winners.jsonl').open('w', encoding='utf-8') as f:
        f.write(json.dumps(judge, ensure_ascii=False) + "\n")
    # Write a minimal leaderboard CSV: id,score (1.0 for winner, 0.0 otherwise)
    variants = res.get("variants") or []
    best_id = (judge or {}).get("best_id")
    with (outdir / 'leaderboard.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["variant_id", "score", "note"]) 
        for idx, v in enumerate(variants, start=1):
            vid = v.get("id") or f"variant_{idx}"
            score = 1.0 if vid == best_id else 0.0
            w.writerow([vid, score, "winner" if score == 1.0 else ""])
    print(json.dumps({
        "variants_count": len(variants),
        "winner": best_id,
        "winners_path": str(outdir / 'winners.jsonl'),
        "leaderboard_path": str(outdir / 'leaderboard.csv'),
    }, ensure_ascii=False))

if __name__ == '__main__':
    main()

