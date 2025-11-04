import sys
import asyncio


def test_shutdown_idempotent(monkeypatch):
    # Provide a minimal litellm shim with closeable client
    class AClient:
        async def close(self):
            return None

    class BHandler:
        async def close(self):
            return None

    class _LW:
        class LoggingWorker:
            async def stop(self):
                return None

        GLOBAL_LOGGING_WORKER = LoggingWorker()

    m = type("_L", (), {})()
    m.module_level_aclient = AClient()
    m.request_timeout = 10
    sys.modules['litellm'] = m
    sys.modules['litellm.main'] = type("_LM", (), {"base_llm_aiohttp_handler": BHandler()})
    sys.modules['litellm.litellm_core_utils'] = type("_LCU", (), {"logging_worker": _LW})

    import scillm

    # Should not raise on repeated shutdown
    scillm.shutdown()
    scillm.shutdown()

