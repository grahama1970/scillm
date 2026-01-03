#!/usr/bin/env python3
"""
Thin wrapper to run the SciLLM↔Chutes doctor using the path expected by
pipeline docs and repro steps.

Usage:
  PYTHONPATH=$(pwd)/src python scripts/tools/scillm_quick_doctor.py
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    # Prefer the src/ tree version when available
    cand = Path("src/extractor/scripts/doctor/scillm_chutes_doctor.py")
    if cand.exists():
        runpy.run_path(str(cand), run_name="__main__")
    else:
        # fallback to repo-root variant
        runpy.run_path("extractor/scripts/doctor/scillm_chutes_doctor.py", run_name="__main__")
