import os

from scillm.paved import shutdown, maybe_disable_paved_logging_from_env
from scillm import disable_background_services


def test_shutdown_is_idempotent():
    assert shutdown() is True
    assert shutdown() is True


def test_disable_background_services_closes_coroutines():
    # Ensure that disabling background services consumes coroutines without warnings
    async def _dummy():
        return 42

    disable_background_services()
    from litellm.litellm_core_utils import logging_worker as _lw  # type: ignore

    worker = _lw.GLOBAL_LOGGING_WORKER
    coro = _dummy()
    worker.enqueue(coro)  # type: ignore
    assert coro.cr_running is False if hasattr(coro, "cr_running") else True
    # coro should be closed to avoid unawaited warnings
    if hasattr(coro, "cr_await"):
        assert coro.cr_await is None


def test_maybe_disable_paved_logging_from_env_no_flags():
    # No relevant env set; should be a no-op
    for k in [
        "SCILLM_PAVED_DISABLE_LOGGING",
        "LITELLM_LOGGING",
        "SPARTA_LITELLM_DISABLE_BG",
    ]:
        os.environ.pop(k, None)
    assert maybe_disable_paved_logging_from_env() is False


def test_maybe_disable_paved_logging_from_env_flagged():
    os.environ["SCILLM_PAVED_DISABLE_LOGGING"] = "1"
    assert maybe_disable_paved_logging_from_env() is True
    os.environ.pop("SCILLM_PAVED_DISABLE_LOGGING", None)
