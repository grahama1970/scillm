#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    p = (
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
        .strip()
    )
    return Path(p)


def _tracked_root_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return sorted([p for p in out.splitlines() if p and "/" not in p])


def _read_allowlist(repo_root: Path) -> set[str]:
    allow_path = repo_root / "scripts" / "root_allowlist.txt"
    if not allow_path.exists():
        raise FileNotFoundError(f"missing allowlist: {allow_path}")
    allow: set[str] = set()
    for line in allow_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        allow.add(s)
    return allow


def main() -> int:
    repo_root = _repo_root()
    allow = _read_allowlist(repo_root)
    tracked = _tracked_root_files()

    unknown = [p for p in tracked if p not in allow]
    if not unknown:
        print(f"[root-layout] ok ({len(tracked)} root files tracked)")
        return 0

    print("[root-layout] unexpected tracked files in repo root:")
    for p in unknown:
        print(f"  - {p}")
    print()
    print("Fix options:")
    print("  1) Move files under docs/, scripts/, deploy/, local/, etc.")
    print("  2) If a tool requires the file at root, add it to scripts/root_allowlist.txt")
    return 2


if __name__ == "__main__":
    sys.exit(main())

