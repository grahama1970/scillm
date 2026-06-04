from __future__ import annotations

import re


PATCH_APPLIED = "PATCH_APPLIED"
PATCH_DELEGATE_BLOCKED = "PATCH_DELEGATE_BLOCKED"
PATCH_DELEGATE_INCOMPLETE = "PATCH_DELEGATE_INCOMPLETE"

_MAX_SCAN_CHARS = 20_000

_SUCCESS_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bPATCH_APPLIED\b",
        r"\bpatch(?:es)?\s+(?:applied|landed)\b",
        r"\bapplied\s+(?:the\s+)?patch\b",
        r"\bimplemented\s+(?:the\s+)?(?:fix|change|patch)\b",
        r"\bupdated\s+[^\n.]{0,120}\b(?:file|test|module|src/|tests/)\b",
    )
]

_CONCRETE_BLOCKER_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpython(?:3)?\s+not\s+found\b",
        r"\bpermission\s+denied\b",
        r"\bmissing\s+test\s+runner\b",
        r"\bno\s+writable\s+workspace\b",
        r"\btimeout\s+before\s+tool\s+execution\b",
        r"\b(?:file|directory|path)\s+not\s+found\b",
        r"\bno\s+such\s+file\s+or\s+directory\b",
        r"\bcommand\s+not\s+found\b",
        r"\bmodule\s+not\s+found\b",
        r"\bimporterror\b",
        r"\bmodulenotfounderror\b",
        r"\bread-?only\s+file\s+system\b",
        r"\bnetwork\s+(?:unreachable|timeout|timed\s+out)\b",
        r"\brate\s+limit(?:ed)?\b",
    )
]

_FILE_PATH_PATTERN = re.compile(
    r"(?<![\w./-])(?:src|tests|test|scripts|deploy|local|chutes|services|skills)/[A-Za-z0-9_./+-]+\.[A-Za-z0-9]+"
)
_PATCH_HEADER_PATTERN = re.compile(r"^\*\*\*\s+(?:Begin Patch|Update File:|Add File:|Delete File:).*$", re.MULTILINE)
_DIFF_HEADER_PATTERN = re.compile(r"^(?:diff --git\s+a/\S+\s+b/\S+|---\s+(?:a/)?\S+|\+\+\+\s+(?:b/)?\S+|@@)", re.MULTILINE)


def _bounded_text(text: str) -> str:
    if len(text) <= _MAX_SCAN_CHARS:
        return text
    head = text[: _MAX_SCAN_CHARS // 2]
    tail = text[-(_MAX_SCAN_CHARS // 2) :]
    return f"{head}\n{tail}"


def _unique_markers(markers: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for marker in markers:
        cleaned = marker.strip().rstrip(".,;:)")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def _patch_evidence_markers(text: str) -> list[str]:
    markers: list[str] = []
    markers.extend(match.group(0) for match in _FILE_PATH_PATTERN.finditer(text))
    markers.extend(match.group(0).splitlines()[0] for match in _PATCH_HEADER_PATTERN.finditer(text))
    markers.extend(match.group(0).splitlines()[0] for match in _DIFF_HEADER_PATTERN.finditer(text))
    return _unique_markers(markers)


def _concrete_blocker_reason(text: str) -> str:
    for pattern in _CONCRETE_BLOCKER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return ""


def classify_patch_delegate_result(text: str) -> dict:
    """Classify bounded worker output without trusting unsupported success claims."""
    if not text or not text.strip():
        return {
            "status": PATCH_DELEGATE_INCOMPLETE,
            "reason": "empty output",
            "has_patch_evidence": False,
            "has_concrete_blocker": False,
            "evidence_markers": [],
        }

    scanned = _bounded_text(text.strip())
    evidence_markers = _patch_evidence_markers(scanned)
    has_patch_evidence = bool(evidence_markers)
    blocker_reason = _concrete_blocker_reason(scanned)
    has_concrete_blocker = bool(blocker_reason)

    if has_concrete_blocker:
        return {
            "status": PATCH_DELEGATE_BLOCKED,
            "reason": blocker_reason,
            "has_patch_evidence": has_patch_evidence,
            "has_concrete_blocker": True,
            "evidence_markers": evidence_markers,
        }

    has_success_claim = any(pattern.search(scanned) for pattern in _SUCCESS_PATTERNS)
    if has_success_claim and has_patch_evidence:
        return {
            "status": PATCH_APPLIED,
            "reason": "success claim with patch evidence",
            "has_patch_evidence": True,
            "has_concrete_blocker": False,
            "evidence_markers": evidence_markers,
        }

    reason = "missing patch evidence" if has_success_claim else "no complete patch receipt"
    return {
        "status": PATCH_DELEGATE_INCOMPLETE,
        "reason": reason,
        "has_patch_evidence": has_patch_evidence,
        "has_concrete_blocker": False,
        "evidence_markers": evidence_markers,
    }
