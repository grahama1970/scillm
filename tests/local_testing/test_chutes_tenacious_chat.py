import scillm.extras.chutes_simple as cs


class _Flip:
    def __init__(self, n_fail=2):
        self.n = n_fail
        self.calls = 0
    def __call__(self, **kwargs):
        self.calls += 1
        if self.calls <= self.n:
            raise RuntimeError("429 Too Many Requests: capacity")
        class _Msg:
            def get(self, k, d=None):
                return '{"ok":true}' if k == 'content' else d
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
        return _Resp()


def test_tenacious_chat_single_model(monkeypatch):
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test")
    monkeypatch.setenv("CHUTES_TEXT_MODEL", "vendor/Large-235B")
    flip = _Flip(n_fail=2)
    monkeypatch.setattr(cs, "completion", flip)
    monkeypatch.setattr(cs, "_tenacious_sleep", lambda *a, **k: 0)
    r = cs.chutes_chat_json(messages=[{"role":"user","content":"ping"}], tenacious=True, max_wall_time_s=10, backoff_cap_s=1)
    assert hasattr(r, "choices")
    assert flip.calls == 3

