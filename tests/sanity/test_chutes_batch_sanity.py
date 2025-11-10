import json
import os

import pytest

import scripts.sanity.chutes_batch_sanity as mod


def _parse_summary(stdout: str):
    last_json = None
    for line in stdout.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            last_json = stripped
    assert last_json is not None, "No JSON summary found in output"
    return json.loads(last_json)


@pytest.mark.asyncio
async def test_successful_run(monkeypatch, capsys):
    async def fake_acompletions(requests, **kwargs):
        res = []
        for idx, req in enumerate(requests):
            if idx == 0:
                content = {"ok": True}
            elif idx == 1:
                content = {"country": "France", "capital": "Paris"}
            elif idx in (2, 3):
                content = {"description": "panda"}
            else:
                content = {"category": "luminous-harvest"}
            res.append({"request": req, "content": content})
        return res

    monkeypatch.setattr(mod, "parallel_acompletions", fake_acompletions)
    os.environ.setdefault("CHUTES_API_BASE", "http://localhost")
    os.environ.setdefault("CHUTES_API_KEY", "x")
    os.environ.setdefault("CHUTES_TEXT_MODEL", "text")
    os.environ.setdefault("CHUTES_VLM_MODEL", "vlm")

    code = await mod.main(["--execute"])
    captured = capsys.readouterr().out
    summary = _parse_summary(captured)
    assert code == 0
    assert summary["ok"] is True
    assert summary["count"] == 5


@pytest.mark.asyncio
async def test_parallel_acompletions_failure_bubbles(monkeypatch, capsys):
    async def fake_acompletions(requests, **kwargs):
        raise RuntimeError("HTTP 503 Service Unavailable")

    monkeypatch.setattr(mod, "parallel_acompletions", fake_acompletions)
    os.environ.setdefault("CHUTES_API_BASE", "http://localhost")
    os.environ.setdefault("CHUTES_API_KEY", "x")
    os.environ.setdefault("CHUTES_TEXT_MODEL", "text")
    os.environ.setdefault("CHUTES_VLM_MODEL", "vlm")

    code = await mod.main(["--execute"])
    captured = capsys.readouterr().out
    summary = _parse_summary(captured)
    assert code == 1
    assert summary["ok"] is False
    assert "503" in summary["error"]


@pytest.mark.asyncio
async def test_dry_run_preview(monkeypatch, capsys):
    async def fake_acompletions(requests, **kwargs):
        return []

    monkeypatch.setattr(mod, "parallel_acompletions", fake_acompletions)
    os.environ.setdefault("CHUTES_API_BASE", "http://localhost")
    os.environ.setdefault("CHUTES_API_KEY", "x")
    os.environ.setdefault("CHUTES_TEXT_MODEL", "text")
    os.environ.setdefault("CHUTES_VLM_MODEL", "vlm")

    code = await mod.main(["--dry-run"])
    captured = capsys.readouterr().out
    preview = json.loads(captured.strip())
    assert code == 0
    assert preview["mode"] == "dry-run"
    assert preview["count"] == 5
