#!/usr/bin/env python3
from pathlib import Path
import json
import sys


def main() -> int:
    path = Path.home() / ".codex" / "auth.json"
    if not path.exists():
        print(f"Missing: {path}")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Unreadable JSON at {path}: {e}")
        return 2
    keys = list(data.keys())
    print(f"Found {path}. Keys: {keys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

