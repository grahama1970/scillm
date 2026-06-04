"""Unit tests for transport attachment extraction and artifact serving."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scillm.proxy.opencode_transport_attachments import (
    attachment_events_from_sources,
    extract_evidence_case_from_text_chunks,
    register_figure_artifact,
    resolve_served_artifact_path,
    safe_artifact_name,
)


def test_extract_evidence_case_from_json_chunk() -> None:
    payload = {
        "verdict": "satisfied",
        "grade": "A",
        "gates_passed": 3,
        "gates_total": 3,
        "gate_summary": "ok",
        "control_ids": ["CWE-287"],
        "tier": "grounded",
    }
    row = extract_evidence_case_from_text_chunks([json.dumps(payload)])
    assert row is not None
    assert row["verdict"] == "satisfied"
    assert row["control_ids"] == ["CWE-287"]


def test_attachment_events_emit_evidence_and_figure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_DIR", str(tmp_path))
    fig = tmp_path / "charts" / "plot.png"
    fig.parent.mkdir(parents=True)
    fig.write_bytes(b"\x89PNG\r\n\x1a\n")

    evidence = {
        "verdict": "satisfied",
        "gates_passed": 1,
        "gates_total": 1,
        "gate_summary": "pass",
        "control_ids": [],
        "tier": "grounded",
    }
    events = attachment_events_from_sources(
        transport_run_id="otr-test",
        text_chunks=[json.dumps(evidence), f"Wrote {fig} for review."],
        workspace=str(tmp_path),
    )
    types = [e["event_type"] for e in events]
    assert "evidence_case_snapshot" in types
    assert "figure_artifact" in types
    figure_events = [e for e in events if e["event_type"] == "figure_artifact"]
    assert figure_events[0]["figure"]["artifact_name"]
    served = resolve_served_artifact_path("otr-test", figure_events[0]["figure"]["artifact_name"])
    assert served.is_file()


def test_safe_artifact_name_rejects_traversal() -> None:
    with pytest.raises(ValueError):
        safe_artifact_name("../escape.png")
