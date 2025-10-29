import os
import types
from typing import Any, Dict

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
    # Fake a tiny router
    class _FakeRouter:
        def __init__(self, model_list=None, default_litellm_params=None):
            self.model_list = model_list or [{"model_name":"chutes/text","litellm_params":{"api_base":"https://llm.chutes.ai/v1","model":"X/235B"}}]
        def completion(self, **kwargs):
            class _Msg:
                def get(self, k, d=None):
                    return '{"ok":true}' if k == 'content' else d
            class _Choice:
                message = _Msg()
            class _Resp:
                choices = [_Choice()]
            return _Resp()
    monkeypatch.setattr(cs, "auto_router_from_env", lambda **_: _FakeRouter())
    monkeypatch.setattr(cs, "Router", _FakeRouter)
    r = cs.chutes_router_json(messages=[{"role":"user","content":"ping"}])
    assert hasattr(r, "choices")
