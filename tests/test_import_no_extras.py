import sys
import types
import asyncio


def test_import_scillm_without_jinja(monkeypatch):
    # Ensure jinja2 looks absent even if installed
    monkeypatch.setitem(sys.modules, 'jinja2', None)
    if 'litellm' in sys.modules:
        del sys.modules['litellm']
    if 'scillm' in sys.modules:
        del sys.modules['scillm']

    import scillm  # noqa: F401

    assert 'jinja2' not in sys.modules or sys.modules['jinja2'] is None


def test_acompletion_json_minimal(monkeypatch):
    # Mock litellm on-demand import to avoid network
    import importlib

    class _Resp:
        def __init__(self):
            self.choices = [types.SimpleNamespace(message={"content": '{"ok": true}'})]

    class _MockLitellm(types.SimpleNamespace):
        pass

    m = _MockLitellm()
    async def _acompletion(**kwargs):
        return _Resp()

    def _completion(**kwargs):
        return _Resp()

    m.acompletion = staticmethod(_acompletion)
    m.completion = staticmethod(_completion)
    m.request_timeout = 30
    m.disable_aiohttp_transport = False
    m.module_level_aclient = None

    monkeypatch.setitem(sys.modules, 'litellm', m)

    import scillm

    async def _run():
        r = await scillm.acompletion_json(
            model='x',
            messages=[{"role": "user", "content": "{}"}],
            api_base='http://example.com',
            api_key='k',
            max_tokens=16,
        )
        assert r.choices[0].message["content"]

    asyncio.run(_run())

