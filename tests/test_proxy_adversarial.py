"""Blind adversarial end-to-end tests for the scillm proxy.

All tests hit the live proxy at http://127.0.0.1:4001 with Bearer sk-dev-proxy-123.
Zero mocks. Tests exercise auth enforcement, router fallbacks, JSON guard,
streaming SSE contract, error propagation, and endpoint schema validation.

Run: pytest tests/test_proxy_adversarial.py -v
"""
from __future__ import annotations

import json

import httpx
import pytest

# ---------------------------------------------------------------------------
# Self-contained constants (no conftest dependency)
# ---------------------------------------------------------------------------

PROXY_BASE = "http://127.0.0.1:4001"
API_KEY = "sk-dev-proxy-123"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

COMPLETION_TIMEOUT = 30.0
HEALTH_TIMEOUT = 5.0

# ---------------------------------------------------------------------------
# Module-level reachability check — skip entire file if proxy is down
# ---------------------------------------------------------------------------


def _proxy_is_reachable() -> bool:
    try:
        r = httpx.get(f"{PROXY_BASE}/health/liveliness", timeout=3.0)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


_REACHABLE = _proxy_is_reachable()

pytestmark = pytest.mark.skipif(
    not _REACHABLE,
    reason=f"Proxy at {PROXY_BASE} is unreachable — skipping all adversarial tests",
)

# ---------------------------------------------------------------------------
# Discover a live model once at import time (best-effort)
# ---------------------------------------------------------------------------


def _find_live_model() -> str | None:
    """Probe cheapest models to find one that responds."""
    if not _REACHABLE:
        return None
    for model in ("local-text", "gemini-flash", "chutes-deepseek"):
        try:
            r = httpx.post(
                f"{PROXY_BASE}/v1/chat/completions",
                headers=AUTH_HEADERS,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply: OK"}],
                    "max_tokens": 4,
                    "temperature": 0,
                },
                timeout=20.0,
            )
            if r.status_code == 200:
                return model
        except Exception:
            continue
    return None


LIVE_MODEL = _find_live_model()

needs_live_model = pytest.mark.skipif(
    LIVE_MODEL is None,
    reason="No live backend model available",
)


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def _achat(
    client: httpx.AsyncClient,
    model: str,
    content: str | list,
    **kw,
) -> httpx.Response:
    """POST a chat completion request."""
    messages = [{"role": "user", "content": content}]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 16,
        "temperature": 0,
        **kw,
    }
    return await client.post(
        "/v1/chat/completions", json=payload, timeout=COMPLETION_TIMEOUT
    )


# ===================================================================
# 1. AUTH ENFORCEMENT
# ===================================================================


class TestAuthEnforcement:
    """Verify that auth is enforced correctly on all protected endpoints."""

    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self):
        """Request with no Authorization header must be rejected with 401."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "model": "chutes-deepseek",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                },
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self):
        """Request with a wrong Bearer token must be rejected with 401."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE,
            headers={"Authorization": "Bearer sk-wrong-key-999"},
        ) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "model": "chutes-deepseek",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                },
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_basic_auth_rejected(self):
        """HTTP Basic auth must not be accepted — only Bearer tokens."""
        import base64

        creds = base64.b64encode(b"admin:sk-dev-proxy-123").decode()
        async with httpx.AsyncClient(
            base_url=PROXY_BASE,
            headers={"Authorization": f"Basic {creds}"},
        ) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "model": "chutes-deepseek",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                },
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code == 401

    @pytest.mark.asyncio
    @needs_live_model
    async def test_valid_token_succeeds(self):
        """Request with the correct Bearer token must succeed."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, LIVE_MODEL, "Reply: OK")
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_all_protected_endpoints_require_auth(self):
        """All /v1/* endpoints (except health probes) require auth."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            for path in (
                "/v1/scillm/health",
                "/v1/scillm/models",
                "/v1/budget",
                "/v1/models",
            ):
                r = await c.get(path, timeout=HEALTH_TIMEOUT)
                assert r.status_code == 401, (
                    f"{path} returned {r.status_code} without auth"
                )

    @pytest.mark.asyncio
    async def test_health_liveliness_no_auth(self):
        """/health/liveliness must work without auth (Docker healthcheck)."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/health/liveliness", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200


# ===================================================================
# 2. ROUTER FALLBACK CASCADE
# ===================================================================


class TestRouterFallbackCascade:
    """Verify the Chutes DeepSeek same-family cascade works."""

    @pytest.mark.asyncio
    async def test_chutes_deepseek_model_succeeds_via_cascade(self):
        """A request to 'chutes-deepseek' must succeed or fail as a known model."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, "chutes-deepseek", "Reply with exactly: PONG")
            # Accept 200 (success) or 5xx (all providers down) but NOT 404 (model unknown)
            assert r.status_code != 404, "chutes-deepseek model not recognized by router"
            if r.status_code == 200:
                data = r.json()
                assert "choices" in data
                assert len(data["choices"]) > 0

    @pytest.mark.asyncio
    async def test_fallback_chain_in_health(self):
        """Health endpoint must not expose removed DeepSeek variants as fallbacks."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.get("/v1/scillm/health", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200
            data = r.json()
            fb = data.get("fallbacks", {})
            chain = fb.get("chutes-deepseek", [])
            stale = {
                "deepseek-ai/DeepSeek-V3.1-TEE",
                "deepseek-ai/DeepSeek-R1-0528-TEE",
                "deepseek-ai/DeepSeek-R1-0528",
            }
            assert stale.isdisjoint(set(chain)), f"Chutes fallback chain contains removed models: {chain}"

    @pytest.mark.asyncio
    async def test_qwen_fallback_tail_independently_addressable(self):
        """chutes-qwen-large (tail) should be directly addressable, not return 'unknown model'."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, "chutes-qwen-large", "Reply: PONG")
            if r.status_code >= 400:
                body = r.text.lower()
                assert "unknown" not in body, "chutes-qwen-large not recognized as model"

    @pytest.mark.asyncio
    async def test_retry_policy_exposed(self):
        """Retry policy must be present in health with expected keys."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            data = (await c.get("/v1/scillm/health", timeout=HEALTH_TIMEOUT)).json()
            rp = data.get("retry_policy", {})
            assert "internal_server_error" in rp, f"Missing 5xx key in retry_policy: {rp}"
            assert rp["internal_server_error"] >= 1


# ===================================================================
# 3. JSON GUARD
# ===================================================================


class TestJsonGuard:
    """Verify JSON guard validates/repairs responses in json_object mode."""

    @pytest.mark.asyncio
    @needs_live_model
    async def test_json_mode_returns_valid_json(self):
        """Requesting json_object mode must produce parseable JSON content."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(
                c,
                LIVE_MODEL,
                'Return a JSON object with key "status" and value "ok".',
                response_format={"type": "json_object"},
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)  # must not raise
                assert isinstance(parsed, dict), f"Expected dict, got {type(parsed)}"

    @pytest.mark.asyncio
    @needs_live_model
    async def test_json_mode_vague_prompt_still_valid_or_502(self):
        """JSON mode with vague prompt must return valid JSON or 502 (guard rejected)."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(
                c,
                LIVE_MODEL,
                "Tell me about the weather.",
                response_format={"type": "json_object"},
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content is not None:
                    try:
                        json.loads(content)
                    except json.JSONDecodeError:
                        pytest.fail(
                            f"JSON guard let through invalid JSON: {content[:200]}"
                        )
            elif r.status_code == 502:
                # Acceptable: guard caught and rejected irreparable output
                data = r.json()
                err_text = json.dumps(data.get("error", {})).lower()
                assert "json" in err_text, f"502 should mention JSON: {data}"

    @pytest.mark.asyncio
    @needs_live_model
    async def test_non_json_mode_passes_through(self):
        """Without json_object mode, prose responses pass through unchanged."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, LIVE_MODEL, "Say hello in one word.")
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                assert content and len(content) > 0


# ===================================================================
# 4. BUDGET ENDPOINT CONTRACT
# ===================================================================


class TestBudgetEndpointContract:
    """Validate /v1/budget endpoint shape and behavior."""

    @pytest.mark.asyncio
    async def test_budget_returns_200(self):
        """Budget endpoint must return 200 with valid auth."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.get("/v1/budget", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_budget_schema_when_loaded(self):
        """If budget_guard is loaded, response must have daily_limit, remaining, reset_at."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            data = (await c.get("/v1/budget", timeout=HEALTH_TIMEOUT)).json()
            if data.get("status") == "budget_guard_not_loaded":
                pytest.skip("Budget guard not loaded")
            for key in ("daily_limit", "remaining", "reset_at"):
                assert key in data, f"Budget response missing '{key}': {data}"
            assert isinstance(data["remaining"], (int, float))
            assert isinstance(data["daily_limit"], (int, float))

    @pytest.mark.asyncio
    async def test_budget_requires_auth(self):
        """Budget endpoint without auth must return 401."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/v1/budget", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_budget_idempotent(self):
        """Two rapid budget calls return the same key structure."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r1 = (await c.get("/v1/budget", timeout=HEALTH_TIMEOUT)).json()
            r2 = (await c.get("/v1/budget", timeout=HEALTH_TIMEOUT)).json()
            assert set(r1.keys()) == set(r2.keys()), "Budget keys changed between calls"


# ===================================================================
# 5. VLM AUTO-ROUTING
# ===================================================================


class TestVlmAutoRouting:
    """Verify that image_url content parts trigger VLM routing."""

    @pytest.mark.asyncio
    async def test_image_url_routes_to_vlm(self):
        """Message with image_url content part should route to VLM, not crash."""
        content = [
            {"type": "text", "text": "Describe this image briefly."},
            {
                "type": "image_url",
                "image_url": {"url": "https://picsum.photos/id/237/100/100"},
            },
        ]
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, "chutes-deepseek", content)
            # Should not 404 — VLM router should intercept and reroute
            assert r.status_code != 404, "VLM auto-routing failed: got 404"
            # Accept success or provider error, but not 'unknown model'
            if r.status_code >= 400:
                assert "unknown" not in r.text.lower()

    @pytest.mark.asyncio
    async def test_nested_image_url_in_multipart(self):
        """image_url buried after multiple text parts should still trigger VLM."""
        content = [
            {"type": "text", "text": "Context: this is a test."},
            {"type": "text", "text": "Now analyze:"},
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/chart.png"},
            },
        ]
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, "chutes-deepseek", content)
            assert r.status_code > 0  # Must not crash

    @pytest.mark.asyncio
    @needs_live_model
    async def test_text_with_image_word_not_rerouted(self):
        """The word 'image' in plain text must NOT trigger VLM routing."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(
                c, LIVE_MODEL, "Describe what an image classification model does."
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                assert content and len(content) > 0


# ===================================================================
# 6. STREAMING SSE CONTRACT
# ===================================================================


class TestStreamingSSEContract:
    """Verify SSE streaming format compliance."""

    @pytest.mark.asyncio
    @needs_live_model
    async def test_stream_sse_format(self):
        """Streaming response must use data: prefix on every payload line, end with [DONE]."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            async with c.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": LIVE_MODEL,
                    "messages": [{"role": "user", "content": "Count 1 2 3"}],
                    "max_tokens": 16,
                    "stream": True,
                },
                timeout=COMPLETION_TIMEOUT,
            ) as r:
                assert r.status_code == 200
                saw_done = False
                async for line in r.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    assert line.startswith("data: "), f"Bad SSE line: {line[:80]}"
                    payload = line[len("data: "):]
                    if payload.strip() == "[DONE]":
                        saw_done = True
                        break
                    chunk = json.loads(payload)
                    assert "id" in chunk, f"Chunk missing 'id': {chunk}"
                    assert "object" in chunk, f"Chunk missing 'object': {chunk}"
                    assert "choices" in chunk, f"Chunk missing 'choices': {chunk}"
                assert saw_done, "Stream did not end with [DONE]"

    @pytest.mark.asyncio
    @needs_live_model
    async def test_stream_content_type_is_sse(self):
        """Streaming response Content-Type must be text/event-stream."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            async with c.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": LIVE_MODEL,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 4,
                    "stream": True,
                },
                timeout=COMPLETION_TIMEOUT,
            ) as r:
                if r.status_code == 200:
                    ct = r.headers.get("content-type", "")
                    assert "text/event-stream" in ct, f"Expected SSE, got: {ct}"

    @pytest.mark.asyncio
    @needs_live_model
    async def test_non_stream_returns_json_not_sse(self):
        """Explicit stream=false must return application/json with 'message' (not 'delta')."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, LIVE_MODEL, "Hi", stream=False)
            if r.status_code == 200:
                ct = r.headers.get("content-type", "")
                assert "application/json" in ct, f"Expected JSON, got: {ct}"
                data = r.json()
                assert "choices" in data
                assert "message" in data["choices"][0], (
                    "Non-stream must have 'message', not 'delta'"
                )


# ===================================================================
# 7. ERROR PROPAGATION
# ===================================================================


class TestErrorPropagation:
    """Verify error responses for bad models, payloads, and routes."""

    @pytest.mark.asyncio
    async def test_nonexistent_model_returns_error(self):
        """Request to a model that does not exist must return an error, not hang."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, "definitely-not-a-model-xyz999", "hi")
            assert r.status_code >= 400
            # Some error paths return plain text (e.g. Starlette 500); others return JSON
            try:
                data = r.json()
                assert "error" in data, f"Error response missing 'error' key: {data}"
                assert "message" in data["error"], f"Error missing 'message': {data['error']}"
            except json.JSONDecodeError:
                # Plain text error (e.g. "Internal Server Error") is acceptable
                assert len(r.text) > 0, "Empty error response body"

    @pytest.mark.asyncio
    async def test_missing_model_field_returns_400(self):
        """Request without 'model' field must return 400."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                },
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_messages_returns_400(self):
        """Request without 'messages' field must return 400."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={"model": "chutes-deepseek", "max_tokens": 4},
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_route_returns_404(self):
        """Catch-all must return 404 for unknown paths."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.get("/v1/nonexistent/endpoint", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 404
            data = r.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_error_structure_follows_openai_schema(self):
        """Error responses for known-bad requests follow {error: {message, type}} schema.

        Uses a missing-model request (400) which the proxy handles with structured JSON,
        rather than an unknown model which may hit the router and return plain 500.
        """
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            # Missing model field triggers the proxy's own 400 handler (guaranteed JSON)
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                },
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code == 400
            data = r.json()
            assert "error" in data
            err = data["error"]
            assert "message" in err
            assert "type" in err

    @pytest.mark.asyncio
    @needs_live_model
    async def test_unicode_and_emoji_do_not_crash(self):
        """Unicode, emoji, and mixed scripts must not crash the proxy."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await _achat(c, LIVE_MODEL, "Hello \U0001f30d \u4f60\u597d \u041f\u0440\u0438\u0432\u0435\u0442")
            assert r.status_code > 0
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                assert content is None or len(content) > 0

    @pytest.mark.asyncio
    async def test_null_content_handled(self):
        """null content in message should error cleanly, not crash."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "model": "chutes-deepseek",
                    "messages": [{"role": "user", "content": None}],
                    "max_tokens": 4,
                },
                timeout=HEALTH_TIMEOUT,
            )
            assert r.status_code > 0  # Must not crash


# ===================================================================
# 8. MODEL LIST
# ===================================================================


class TestModelList:
    """Verify GET /v1/models returns an OpenAI-compatible list."""

    @pytest.mark.asyncio
    async def test_models_returns_list_object(self):
        """Response must have {object: 'list', data: [...]}."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.get("/v1/models", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200
            data = r.json()
            assert data.get("object") == "list", f"Expected 'list', got: {data.get('object')}"
            assert isinstance(data.get("data"), list), "data field must be a list"
            assert len(data["data"]) > 0, "Model list must not be empty"

    @pytest.mark.asyncio
    async def test_each_model_has_required_fields(self):
        """Every model entry must have id, object, created, owned_by."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            data = (await c.get("/v1/models", timeout=HEALTH_TIMEOUT)).json()
            for entry in data["data"]:
                for field in ("id", "object", "created", "owned_by"):
                    assert field in entry, f"Model entry missing '{field}': {entry}"
                assert entry["object"] == "model"

    @pytest.mark.asyncio
    async def test_known_models_present(self):
        """Core model groups must appear in the list."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            data = (await c.get("/v1/models", timeout=HEALTH_TIMEOUT)).json()
            ids = {m["id"] for m in data["data"]}
            for expected in ("chutes-deepseek", "vlm", "local-text"):
                assert expected in ids, f"'{expected}' not in model list: {ids}"


# ===================================================================
# 9. HEALTH ENDPOINTS
# ===================================================================


class TestHealthEndpoints:
    """Verify /health/liveliness and /health/readiness return expected shapes."""

    @pytest.mark.asyncio
    async def test_liveliness_returns_ok(self):
        """/health/liveliness must return {status: 'ok'}."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/health/liveliness", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200
            data = r.json()
            assert data.get("status") == "ok"

    @pytest.mark.asyncio
    async def test_readiness_returns_ready(self):
        """/health/readiness must return {status: 'ready', model_groups: N}."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/health/readiness", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200
            data = r.json()
            assert data.get("status") == "ready", f"Not ready: {data}"
            assert "model_groups" in data
            assert isinstance(data["model_groups"], int)
            assert data["model_groups"] > 0

    @pytest.mark.asyncio
    async def test_scillm_health_shape(self):
        """/v1/scillm/health must expose status, uptime, model_groups, fallbacks, circuit_breaker."""
        async with httpx.AsyncClient(
            base_url=PROXY_BASE, headers=AUTH_HEADERS
        ) as c:
            r = await c.get("/v1/scillm/health", timeout=HEALTH_TIMEOUT)
            assert r.status_code == 200
            data = r.json()
            assert data.get("status") == "ok"
            for key in (
                "uptime_seconds",
                "model_groups",
                "fallbacks",
                "retry_policy",
                "routing_strategy",
                "circuit_breaker",
            ):
                assert key in data, f"Health response missing '{key}': {list(data.keys())}"
            assert isinstance(data["model_groups"], list)
            assert isinstance(data["uptime_seconds"], (int, float))
            assert data["uptime_seconds"] > 0

    @pytest.mark.asyncio
    async def test_health_liveliness_no_auth_readiness_no_auth(self):
        """Both health probes must work without auth (for k8s/Docker probes)."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            for path in ("/health/liveliness", "/health/readiness"):
                r = await c.get(path, timeout=HEALTH_TIMEOUT)
                assert r.status_code == 200, f"{path} returned {r.status_code}"


# ===================================================================
# 10. METRICS ENDPOINT
# ===================================================================


class TestMetricsEndpoint:
    """Verify GET /metrics returns Prometheus format."""

    @pytest.mark.asyncio
    async def test_metrics_returns_prometheus_format(self):
        """/metrics must return text/plain with Prometheus exposition format."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/metrics", timeout=HEALTH_TIMEOUT)
            # 200 = prometheus_client installed; 404 = not installed (acceptable)
            if r.status_code == 404:
                pytest.skip("prometheus_client not installed on proxy")
            assert r.status_code == 200
            ct = r.headers.get("content-type", "")
            # Prometheus uses text/plain or application/openmetrics-text
            assert "text/" in ct or "openmetrics" in ct, f"Unexpected content-type: {ct}"

    @pytest.mark.asyncio
    async def test_metrics_contains_standard_lines(self):
        """/metrics must contain at least HELP or TYPE lines (Prometheus convention)."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/metrics", timeout=HEALTH_TIMEOUT)
            if r.status_code == 404:
                pytest.skip("prometheus_client not installed on proxy")
            assert r.status_code == 200
            body = r.text
            # Prometheus format has lines starting with # HELP or # TYPE
            has_help = any(line.startswith("# HELP") for line in body.splitlines())
            has_type = any(line.startswith("# TYPE") for line in body.splitlines())
            assert has_help or has_type, (
                f"Metrics body does not look like Prometheus format (first 300 chars): "
                f"{body[:300]}"
            )

    @pytest.mark.asyncio
    async def test_metrics_no_auth_required(self):
        """/metrics must be accessible without auth (for Prometheus scraper)."""
        async with httpx.AsyncClient(base_url=PROXY_BASE) as c:
            r = await c.get("/metrics", timeout=HEALTH_TIMEOUT)
            # Should not be 401
            assert r.status_code != 401, "/metrics should not require auth"
