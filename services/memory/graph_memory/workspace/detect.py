from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set

import typer


STRONG_FILES = {
    "package.json",
    "pnpm-workspace.yaml",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "go.mod",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "mix.exs",
}

WEAK_FILES = {
    ".git",
    ".hg",
    ".svn",
    "README.md",
    "README.rst",
    "Makefile",
    ".vscode",
    ".idea",
    ".env",
}


@dataclass
class Detection:
    root: Path
    reason: str
    strong_hits: List[str]
    weak_hits: List[str]
    scope: str


def detect_workspace(start: Path) -> Detection:
    start = start.resolve()
    best_weak = (0, start, [])
    for candidate in [start, *start.parents]:
        entries: Set[str] = {p.name for p in candidate.iterdir()} if candidate.exists() else set()
        strong = sorted(list(STRONG_FILES & entries))
        weak = sorted(list(WEAK_FILES & entries))
        if strong:
            return Detection(
                root=candidate,
                reason=f"strong:{','.join(strong)}",
                strong_hits=strong,
                weak_hits=weak,
                scope=f"workspace:{candidate.name}",
            )
        if len(weak) > best_weak[0]:
            best_weak = (len(weak), candidate, weak)
    # fallback to best weak if any, else start
    weak_count, weak_root, weak_hits = best_weak
    root = weak_root if weak_count > 0 else start
    return Detection(
        root=root,
        reason="weak" if weak_count > 0 else "fallback",
        strong_hits=[],
        weak_hits=weak_hits,
        scope=f"workspace:{root.name}",
    )


app = typer.Typer(add_completion=False)


@app.command()
def detect(
    start: Path = typer.Argument(Path("."), exists=True, readable=True),
    json_out: bool = typer.Option(True, "--json/--no-json", help="Emit JSON (default)"),
):
    d = detect_workspace(start)
    out = {
        "root": str(d.root),
        "scope": d.scope,
        "reason": d.reason,
        "strong_hits": d.strong_hits,
        "weak_hits": d.weak_hits,
    }
    if json_out:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"root={out['root']} scope={out['scope']} reason={out['reason']} strong={out['strong_hits']} weak={out['weak_hits']}")


if __name__ == "__main__":
    app()

