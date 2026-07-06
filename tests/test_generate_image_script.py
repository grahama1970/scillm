from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_image import (
    PROMPT_MAX_CHARS,
    _default_timeout_s,
    _load_prompt,
    generate_image_codex_oauth,
    generate_image_openai_api,
)


def test_load_prompt_rejects_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.md"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        _load_prompt(path)


def test_load_prompt_rejects_too_long(tmp_path: Path) -> None:
    path = tmp_path / "big.md"
    path.write_text("x" * (PROMPT_MAX_CHARS + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        _load_prompt(path)


def test_generate_image_openai_api_writes_png_and_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("A red circle on white", encoding="utf-8")
    out = tmp_path / "out" / "circle.png"
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00\x90\x91h6\x00\x00\x00\x00IEND\xaeB`\x82"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert request.headers["x-caller-skill"] == "test-skill"
        body = json.loads(request.content.decode())
        assert body["model"] == "gpt-image-2"
        assert body["quality"] == "high"
        return httpx.Response(
            200,
            json={
                "created": 1,
                "object": "list",
                "model": "gpt-image-2",
                "provider": "openai",
                "data": [{"b64_json": base64.b64encode(png).decode("ascii")}],
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs["base_url"] = "http://test"
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)

    receipt = generate_image_openai_api(
        prompt_file=prompt_file,
        out=out,
        base_url="http://test",
        master_key="test-key",
        caller_skill="test-skill",
    )

    assert out.is_file()
    assert receipt["auth"] == "openai-api-key"
    assert receipt["width"] == 16
    assert receipt["height"] == 16


def test_codex_oauth_streams_events_out_before_process_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("A blue square on white", encoding="utf-8")
    out = tmp_path / "image.png"
    receipt_path = tmp_path / "image_receipt.json"
    events_out = tmp_path / "events.jsonl"
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00"
        b"\x90\x91h6\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    png_sha = hashlib.sha256(png).hexdigest()
    events = [
        {"type": "thread.started", "thread_id": "thread-live"},
        {"type": "item.completed", "item": {"type": "image_generation"}},
    ]

    class FakeProc:
        def __init__(self, *args, **kwargs) -> None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(png)
            receipt_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "auth": "codex-oauth",
                        "path": str(out),
                        "width": 16,
                        "height": 16,
                        "sha256": png_sha,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            read_fd, write_fd = os.pipe()
            with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
                for event in events:
                    writer.write(json.dumps(event) + "\n")
            self.stdout = os.fdopen(read_fd, "r", encoding="utf-8")
            self.stderr = io.StringIO("")

        def wait(self, timeout: float | None = None) -> int:
            persisted = [
                json.loads(line)
                for line in events_out.read_text(encoding="utf-8").splitlines()
            ]
            assert persisted == events
            return 0

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    monkeypatch.setattr("scripts.generate_image._codex_oauth_available", lambda: True)
    monkeypatch.setattr("subprocess.Popen", FakeProc)

    receipt = generate_image_codex_oauth(
        prompt_file=prompt_file,
        out=out,
        cwd=tmp_path,
        receipt_path=receipt_path,
        timeout_s=45.0,
        first_event_s=5.0,
        events_out=events_out,
    )

    assert receipt["auth"] == "codex-oauth"
    persisted = [json.loads(line) for line in events_out.read_text(encoding="utf-8").splitlines()]
    assert persisted == events


def test_default_timeout_s_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_IMAGE_TIMEOUT_S", "1234")

    assert _default_timeout_s() == 1234.0
