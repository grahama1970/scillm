#!/usr/bin/env python3
import os
from pathlib import Path


def load_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def main() -> int:
    out_dir = Path("docs/review_competition")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Candidates
    baseline = out_dir / "01_baseline.md"
    scillm = out_dir / "02_gpt5_high.md"

    base_txt = load_text(baseline)
    sci_txt = load_text(scillm)

    comparison = ["# Review Comparison\n"]

    if base_txt and sci_txt:
        comparison.append("- Candidates: baseline vs. codex-agent (SciLLM)\n")
        # Very lightweight heuristic: pick longer, more specific review
        winner = "02_gpt5_high.md" if len(sci_txt) >= len(base_txt) else "01_baseline.md"
        comparison.append(f"- Winner: {winner}\n")
    elif sci_txt:
        comparison.append("- Only one candidate present: codex-agent review.\n")
        comparison.append("- Winner: 02_gpt5_high.md\n")
    else:
        comparison.append("- No candidate reviews found.\n")

    out = out_dir / "03_comparison.md"
    out.write_text("\n".join(comparison), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

