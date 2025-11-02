import sys, types, json, asyncio, os

# Stub orjson used by proxy helpers
sys.modules['orjson'] = types.SimpleNamespace(loads=json.loads, dumps=json.dumps)

from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
from litellm.proxy._types import UserAPIKeyAuth, ProxyException
from litellm.proxy.utils import ProxyLogging
from litellm.exceptions import RateLimitError


class DummyCache:
    pass


def _dummy_user():
    return UserAPIKeyAuth(api_key='x', user_id='u', user_role='proxy_admin', team_id=None, spend=0.0, max_budget=1e9, tpm_limit=0, rpm_limit=0)  # type: ignore


def test_get_custom_headers_includes_budget_headers(monkeypatch):
    os.environ['CHUTES_API_BASE'] = 'https://api.chutes.ai'
    os.environ['CHUTES_API_KEY'] = 'k'
    os.environ['CHUTES_DAILY_LIMIT'] = '10'
    h = ProxyBaseLLMRequestProcessing.get_custom_headers(user_api_key_dict=_dummy_user())
    assert 'x-ratelimit-remaining-requests' in h
    assert 'x-budget-reset-at' in h


def test_429_budget_exception_contains_hint(monkeypatch):
    os.environ['SCILLM_COOLDOWN_429_S']='5'
    os.environ['CHUTES_API_BASE'] = 'https://api.chutes.ai'
    os.environ['CHUTES_API_KEY'] = 'k'
    # Seed tracker once to produce a snapshot
    from chutes.middleware.budget_guard import budget_register_attempt
    budget_register_attempt()

    proc = ProxyBaseLLMRequestProcessing(data={})
    user = _dummy_user()
    log = ProxyLogging(user_api_key_cache=DummyCache())

    # Craft RateLimitError with reset header
    import datetime as dt
    reset = (dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(seconds=30)).isoformat()+'Z'
    err = RateLimitError(message='Daily API call limit exceeded', llm_provider='openai_like', model='m')
    # attach headers
    err.response = types.SimpleNamespace(headers={'Retry-After':'5','x-budget-reset-at': reset})

    try:
        asyncio.run(proc._handle_llm_api_exception(err, user_api_key_dict=user, proxy_logging_obj=log, version='x'))
    except ProxyException as ex:
        assert ex.code == '429'
        assert 'budget' in ex.type or ex.type == 'budget_exhausted'
        # message includes hint fields when snapshot is present
        assert 'remaining=' in ex.message and 'reset_at=' in ex.message
        assert ex.headers.get('Retry-After') == '5'
        assert 'x-budget-reset-at' in ex.headers

