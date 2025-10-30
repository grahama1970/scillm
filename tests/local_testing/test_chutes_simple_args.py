import os
import types
from typing import Any, Dict

import pytest
from litellm.exceptions import APIError

import scillm.extras.chutes_simple as cs


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        class _Msg:
            def get(self, k, d=None):
                return '{"ok":true}' if k == 'content' else d
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
        return _Resp()


def test_chutes_chat_json_builds_bearer_headers(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "foo/Bar-235B-Instruct")
    spy = _Spy()
    monkeypatch.setattr(cs, "completion", spy)
    cs.chutes_chat_json(messages=[{"role":"user","content":"ping"}])
    call = spy.calls[-1]
    assert call["custom_llm_provider"] == "openai_like"
    assert call["api_key"] is None
    assert call["extra_headers"]["Authorization"].startswith("Bearer ")


def test_chutes_router_json_adds_headers(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "foo/Bar-235B-Instruct")
    monkeypatch.delenv("CHUTES_TEXT_MODEL_ALT1", raising=False)
    monkeypatch.delenv("CHUTES_TEXT_MODEL_ALT2", raising=False)
    monkeypatch.delenv("CHUTES_VLM_MODEL", raising=False)
    monkeypatch.delenv("CHUTES_VLM_MODEL_ALT1", raising=False)
    monkeypatch.delenv("CHUTES_VLM_MODEL_ALT2", raising=False)
    # Fake a tiny router
    class _FakeRouter:
        def __init__(self, model_list=None, default_litellm_params=None):
            self.model_list = model_list or []
            self.default_litellm_params = default_litellm_params or {}
            self.closed = False
            self.calls = 0

        def completion(self, **kwargs):
            self.calls += 1
            class _Msg:
                def get(self, k, d=None):
                    return '{"ok":true}' if k == 'content' else d

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

        def close(self):
            self.closed = True

    fake = _FakeRouter()
    monkeypatch.setattr(cs, "Router", lambda **kwargs: fake)
    resp = cs.chutes_router_json(messages=[{"role":"user","content":"ping"}])
    assert hasattr(resp, "choices")
    assert fake.closed is True
    assert fake.calls == 1


def test_chutes_router_json_rejects_alternates(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "foo/Bar-235B-Instruct")
    monkeypatch.setenv("CHUTES_TEXT_MODEL_ALT1", "foo/Alt")
    with pytest.raises(RuntimeError) as excinfo:
        cs.chutes_router_json(messages=[{"role":"user","content":"ping"}])
    assert "LOCKED" in str(excinfo.value)


def test_chutes_router_json_retries_transient(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "foo/Bar-235B-Instruct")
    monkeypatch.delenv("CHUTES_TEXT_MODEL_ALT1", raising=False)
    monkeypatch.delenv("CHUTES_TEXT_MODEL_ALT2", raising=False)
    monkeypatch.delenv("CHUTES_VLM_MODEL", raising=False)
    monkeypatch.delenv("CHUTES_VLM_MODEL_ALT1", raising=False)
    monkeypatch.delenv("CHUTES_VLM_MODEL_ALT2", raising=False)
    monkeypatch.setattr(cs, "_sleep_with_backoff", lambda attempt, hint: 0)

    class _FakeRouter:
        def __init__(self, model_list=None, default_litellm_params=None):
            self.calls = 0
            self.closed = False

        def completion(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                err = APIError(503, "server 503", "chutes", "foo/Bar-235B-Instruct")
                raise err

            class _Msg:
                def get(self, k, d=None):
                    return '{"ok":true}' if k == 'content' else d

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

        def close(self):
            self.closed = True

    fake = _FakeRouter()
    monkeypatch.setattr(cs, "Router", lambda **kwargs: fake)
    resp = cs.chutes_router_json(messages=[{"role": "user", "content": "ping"}], max_retries=2)
    assert fake.calls == 2
    assert getattr(resp, "scillm_meta", {}).get("attempts") == 2
    assert fake.closed is True


def test_chutes_healthcheck(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "foo/Bar-235B-Instruct")

    class _Resp:
        def __init__(self):
            self.model = "foo/Bar-235B-Instruct"
            self.choices = []

    monkeypatch.setattr(cs, "chutes_chat_json", lambda **_: _Resp())
    result = cs.chutes_healthcheck()
    assert result["ok"] is True
    assert result["served_model"] == "foo/Bar-235B-Instruct"
