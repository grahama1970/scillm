import json
from pathlib import Path

from scillm.harness.opencode_transport_campaign_canary import classify_campaign


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _append(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_classify_campaign_requires_retry_timeout_abort_and_blocked(tmp_path: Path) -> None:
    _write_json(tmp_path / "terminal_proof.json", {"status": "complete"})
    ledger = tmp_path / "attempts.jsonl"
    _append(ledger, {"step": "retry_attempt_1", "validation_passed": False})
    _append(ledger, {"step": "retry_attempt_2", "validation_passed": True, "fork_supersede": True})
    _append(ledger, {"step": "timeout_probe", "delivery_state": "timed_out", "finish_marker_exists": False})
    _append(ledger, {"step": "abort_probe", "delivery_state": "aborted", "started_seen": True, "finish_marker_exists": False})
    _append(
        ledger,
        {
            "step": "blocked_probe",
            "delivery_state": "blocked",
            "blocked_reason": "command_not_found",
            "marker_exists": False,
        },
    )

    result = classify_campaign(tmp_path)

    assert result["pass"] is True
    assert result["retry_ok"] is True
    assert result["timeout_ok"] is True
    assert result["abort_ok"] is True
    assert result["blocked_ok"] is True


def test_classify_campaign_fails_without_concrete_blocked_reason(tmp_path: Path) -> None:
    _write_json(tmp_path / "terminal_proof.json", {"status": "complete"})
    ledger = tmp_path / "attempts.jsonl"
    _append(ledger, {"step": "retry_attempt_1", "validation_passed": False})
    _append(ledger, {"step": "retry_attempt_2", "validation_passed": True, "fork_supersede": True})
    _append(ledger, {"step": "timeout_probe", "delivery_state": "timed_out", "finish_marker_exists": False})
    _append(ledger, {"step": "abort_probe", "delivery_state": "aborted", "started_seen": True, "finish_marker_exists": False})
    _append(ledger, {"step": "blocked_probe", "delivery_state": "blocked", "blocked_reason": "", "marker_exists": False})

    result = classify_campaign(tmp_path)

    assert result["pass"] is False
    assert result["blocked_ok"] is False
