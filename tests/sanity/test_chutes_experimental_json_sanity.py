import json
import os

import pytest

import scripts.sanity.chutes_experimental_json_sanity as mod


def _parse_summary(stdout: str):
    last_json = None
    for line in stdout.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            last_json = stripped
    assert last_json is not None, "No JSON summary found in output"
    return json.loads(last_json)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "http://localhost")
    monkeypatch.setenv("CHUTES_API_KEY", "test-key")
    monkeypatch.setenv("CHUTES_EXPERIMENTAL", "moonshotai/Kimi-K2-Thinking")


@pytest.mark.asyncio
async def test_successful_run(monkeypatch, capsys):
    async def fake_iter(requests, **kwargs):
        payloads = [
            {"ok": True},
            {"problem": "17+28+13", "answer": 58, "explanation": "added"},
            {"country": "France", "capital": "Paris", "continent": "Europe"},
            {
                "steps": [
                    {"id": 1, "task": "inventory", "owner": "dev"},
                    {"id": 2, "task": "migrate", "owner": "ops"},
                    {"id": 3, "task": "verify", "owner": "qa"},
                ],
                "confidence": "high",
            },
            {
                "scores": [
                    {"option": "low_latency", "score": 0.7, "justification": "fast"},
                    {"option": "high_accuracy", "score": 0.6, "justification": "precise"},
                ],
                "winner": "low_latency",
            },
        ]
        for idx, payload in enumerate(payloads):
            resp = {"choices": [{"message": {"content": json.dumps(payload)}}]}
            yield {
                "index": idx,
                "request": requests[idx],
                "ok": True,
                "response": resp,
                "attempts": 1,
                "elapsed_s": 0.1,
            }

    monkeypatch.setattr(mod, "parallel_acompletions_iter", fake_iter)
    code = await mod.main(["--execute", "--json-summary"])
    captured = capsys.readouterr().out
    summary = _parse_summary(captured)
    assert code == 0
    assert summary["ok"] is True
    assert summary["count"] == 5
    assert summary["model"] == "moonshotai/Kimi-K2-Thinking"


@pytest.mark.asyncio
async def test_invalid_json_failure(monkeypatch, capsys):
    async def fake_iter(requests, **kwargs):
        # First scenario returns malformed JSON
        resp_bad = {"choices": [{"message": {"content": "{not-json"}}]}
        yield {"index": 0, "request": requests[0], "ok": True, "response": resp_bad}
        # Remaining scenarios succeed with dummy payloads
        for idx in range(1, len(requests)):
            payload = {"ok": True}
            resp = {"choices": [{"message": {"content": json.dumps(payload)}}]}
            yield {"index": idx, "request": requests[idx], "ok": True, "response": resp}

    monkeypatch.setattr(mod, "parallel_acompletions_iter", fake_iter)
    code = await mod.main(["--execute", "--no-json-sanitize", "--json-summary"])
    captured = capsys.readouterr().out
    summary = _parse_summary(captured)
    assert code == 1
    assert summary["ok"] is False
    assert summary["failure_count"] >= 1
    reasons = [item["reason"] for item in summary["items"]]
    assert "invalid_json" in reasons


@pytest.mark.asyncio
async def test_dry_run(monkeypatch, capsys):
    code = await mod.main(["--dry-run"])
    captured = capsys.readouterr().out.strip()
    preview = json.loads(captured)
    assert code == 0
    assert preview["mode"] == "dry-run"
    assert preview["count"] == 5
    assert "chutes/experimental" == preview["model_alias"]
