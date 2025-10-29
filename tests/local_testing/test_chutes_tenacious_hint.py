import pytest
import scillm.extras.chutes_simple as cs


def test_tenacious_nonretryable_appends_hint(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "vendor/Large-235B")

    def _boom(**kwargs):
        raise RuntimeError("400 Bad Request: invalid schema")

    monkeypatch.setattr(cs, "completion", _boom)
    monkeypatch.setattr(cs, "_tenacious_sleep", lambda *a, **k: 0)

    with pytest.raises(RuntimeError) as ei:
        cs.chutes_chat_json(messages=[{"role":"user","content":"ping"}], tenacious=True, max_wall_time_s=1)
    assert "not retried: auth/mapping/schema" in str(ei.value)

