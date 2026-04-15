from __future__ import annotations
import re
from typing import Optional

_NUM = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")

def param_nl(text: Optional[str]) -> Optional[str]:
    """Parameterize an NL requirement for retrieval.

    - Replace bare numbers with <N>
    - Normalize unicode inequalities ≤/≥ to <=/>=
    - Collapse whitespace
    """
    if not text:
        return text
    t = _NUM.sub("<N>", text)
    t = t.replace("≤", "<=").replace("≥", ">=")
    t = _WS.sub(" ", t).strip()
    return t

