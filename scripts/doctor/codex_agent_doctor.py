#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    # Reuse debug doctor implementation
    sys.exit(runpy.run_path("debug/codex_agent_doctor.py"))

