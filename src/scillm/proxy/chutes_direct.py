"""Direct Chutes.ai passthrough — zero middleware, one httpx call.

Bypasses the entire middleware chain (ChutesRouter, ConcurrencyGuard,
TimeoutEstimator, JsonGuard, etc.) and calls Chutes directly. Same
behavior as ``curl -X POST https://llm.chutes.ai/v1/chat/completions``.

Usage (single):
    POST /v1/scillm/chutes/completions
    Body: {"model": "deepseek-ai/DeepSeek-V3.2-TEE", "messages": [...], "stream": true}

Usage (batch):
    POST /v1/scillm/chutes/batch
    Body: {"requests": [{"model": "...", "messages": [...]}, ...], "concurrency": 4}
    Response: SSE stream of completion events via asyncio.as_completed

Hot/cold check:
    Before calling, fetches ``/v1/models`` from Chutes. The requested model must
    be an exact live model ID; aliases and same-family substitutions are not
    resolved here.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from loguru import logger

_RAW_CHUTES_API_BASE = os.environ.get("CHUTES_API_BASE", "https://llm.chutes.ai").rstrip("/")
# Strip /v1 suffix if present — we append our own paths
if _RAW_CHUTES_API_BASE.endswith("/v1"):
    _RAW_CHUTES_API_BASE = _RAW_CHUTES_API_BASE[:-3]
_CHUTES_INFERENCE_BASE = _RAW_CHUTES_API_BASE
_CHUTES_API_KEY = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN", "")
_OPS_CHUTES_DIR = Path(
    os.environ.get(
        "SCILLM_OPS_CHUTES_DIR",
        "/home/graham/workspace/experiments/agent-skills/skills/ops-chutes",
    )
)
_OPS_CHUTES_RUN = Path(os.environ.get("SCILLM_OPS_CHUTES_RUN", str(_OPS_CHUTES_DIR / "run.sh")))
_OPS_CHUTES_ENABLED = os.environ.get("SCILLM_OPS_CHUTES_ENABLED", "1").lower() not in {"0", "false", "no"}
_OPS_CHUTES_REQUIRE_HOT = os.environ.get("SCILLM_OPS_CHUTES_REQUIRE_HOT", "1").lower() not in {"0", "false", "no"}
if _OPS_CHUTES_DIR.exists():
    sys.path.insert(0, str(_OPS_CHUTES_DIR))
try:
    from throttle import ChutesSemaphore
except Exception:  # pragma: no cover - optional host integration
    ChutesSemaphore = None  # type: ignore[assignment]

_CACHE_TTL_S = 300
_http_client: httpx.AsyncClient | None = None


def resolve_chutes_model_alias(model: str) -> str:
    """Return the Chutes model unchanged.

    The direct Chutes lane intentionally has no model aliases. Callers must use
    an exact model ID selected from live Chutes inventory.
    """
    return model


class _AdaptiveConcurrencyLimiter:
    """Small AIMD-style limiter for one Chutes batch run."""

    def __init__(self, initial_limit: int, *, slow_call_s: float = 30.0) -> None:
        self.max_limit = max(1, int(initial_limit))
        self.limit = self.max_limit
        self.slow_call_s = slow_call_s
        self.in_flight = 0
        self.success_streak = 0
        self.events: list[dict[str, Any]] = []
        self._condition = asyncio.Condition()

    async def __aenter__(self) -> "_AdaptiveConcurrencyLimiter":
        async with self._condition:
            while self.in_flight >= self.limit:
                await self._condition.wait()
            self.in_flight += 1
            return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        async with self._condition:
            self.in_flight = max(0, self.in_flight - 1)
            self._condition.notify_all()

    def record(self, *, status: int, elapsed_s: float, attempt: int) -> None:
        old = self.limit
        reason = ""
        if status == 429:
            self.limit = max(1, self.limit // 2)
            self.success_streak = 0
            reason = "rate_limit"
        elif status in {502, 503, 504}:
            self.limit = max(1, self.limit - 1)
            self.success_streak = 0
            reason = f"http_{status}"
        elif elapsed_s >= self.slow_call_s and self.limit > 1:
            self.limit = max(1, self.limit - 1)
            self.success_streak = 0
            reason = "slow_call"
        elif status == 200:
            self.success_streak += 1
            if self.success_streak >= max(1, self.limit) and self.limit < self.max_limit:
                self.limit += 1
                self.success_streak = 0
                reason = "success_recovery"

        if self.limit != old:
            self.events.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "reason": reason,
                    "old_limit": old,
                    "new_limit": self.limit,
                    "elapsed_s": round(elapsed_s, 3),
                }
            )


async def _run_ops_chutes(*args: str, timeout: float = 60.0) -> dict[str, Any]:
    """Run the ops-chutes CLI and return a structured receipt."""
    cmd = [str(_OPS_CHUTES_RUN), *args]
    if not _OPS_CHUTES_ENABLED:
        return {
            "ok": True,
            "skipped": True,
            "reason": "SCILLM_OPS_CHUTES_ENABLED disabled",
            "cmd": cmd,
            "stdout": "",
            "stderr": "",
            "returncode": 0,
        }
    if not _OPS_CHUTES_RUN.exists():
        return {
            "ok": False,
            "error": "ops_chutes_run_not_found",
            "cmd": cmd,
            "stdout": "",
            "stderr": "",
            "returncode": 127,
        }
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": "ops_chutes_timeout",
            "cmd": cmd,
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "ops_chutes_exec_failed",
            "cmd": cmd,
            "stdout": "",
            "stderr": str(exc),
            "returncode": None,
        }

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode,
    }


def _ops_stdout_json(receipt: dict[str, Any]) -> dict[str, Any] | None:
    text = receipt.get("stdout") or ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return _extract_first_json_object(text)
    return value if isinstance(value, dict) else None


def _stream_delta_from_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """Extract visible/reasoning deltas from one OpenAI-compatible stream chunk."""
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    choices = payload.get("choices") or []
    if not choices:
        return "", "", usage
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    message = choice.get("message") or {}
    content = delta.get("content")
    if content is None:
        content = message.get("content")
    reasoning = delta.get("reasoning_content")
    if reasoning is None:
        reasoning = message.get("reasoning_content")
    return str(content or ""), str(reasoning or ""), usage


async def _collect_streaming_completion(
    resp: httpx.Response,
    *,
    base_item: dict[str, Any],
    best_model: str,
    attempt: int,
    queue: asyncio.Queue[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect a provider SSE response and optionally emit live batch deltas."""
    content_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    usage: dict[str, Any] | None = None
    done_seen = False
    try:
        async for raw_line in resp.aiter_lines():
            line = raw_line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            data_text = line[len("data:"):].strip()
            if data_text == "[DONE]":
                done_seen = True
                break
            try:
                payload = json.loads(data_text)
            except json.JSONDecodeError:
                continue
            content, reasoning, chunk_usage = _stream_delta_from_payload(payload)
            if chunk_usage is not None:
                usage = chunk_usage
            if content:
                content_chunks.append(content)
                if queue is not None:
                    await queue.put(
                        {
                            **base_item,
                            "type": "target_content_delta",
                            "ok": True,
                            "delta": content,
                            "model_served": best_model,
                            "attempt": attempt,
                        }
                    )
            if reasoning:
                reasoning_chunks.append(reasoning)
                if queue is not None:
                    await queue.put(
                        {
                            **base_item,
                            "type": "target_reasoning_delta",
                            "ok": True,
                            "delta": reasoning,
                            "model_served": best_model,
                            "attempt": attempt,
                        }
                    )
    finally:
        await resp.aclose()
        slot = getattr(resp, "_scillm_chutes_slot", None)
        if slot is not None:
            await slot.release()

    if not done_seen:
        raise httpx.RemoteProtocolError("stream ended without [DONE]")
    final_content = "".join(content_chunks) or "".join(reasoning_chunks)
    message: dict[str, Any] = {"role": "assistant", "content": final_content}
    if reasoning_chunks:
        message["reasoning_content"] = "".join(reasoning_chunks)
    data: dict[str, Any] = {
        "choices": [{"message": message}],
        "model": best_model,
    }
    if usage is not None:
        data["usage"] = usage
    return data


def _parse_model_health(receipt: dict[str, Any]) -> str | None:
    text = f"{receipt.get('stdout') or ''}\n{receipt.get('stderr') or ''}".upper()
    for status in ("HOT", "COLD", "DOWN"):
        if status in text:
            return status
    return None


async def _ops_chutes_model_plan(model: str) -> dict[str, Any]:
    """Use ops-chutes to check hot/cold and recommend an exact model."""
    plan: dict[str, Any] = {
        "requested_model": model,
        "model": model,
        "health": None,
        "action": "use_requested",
        "checks": {},
    }
    if not _OPS_CHUTES_ENABLED:
        plan["action"] = "ops_disabled"
        return plan

    health = await _run_ops_chutes("model-health", model, timeout=45.0)
    plan["checks"]["model_health"] = health
    status = _parse_model_health(health)
    plan["health"] = status
    if not health.get("ok"):
        # Checker UNAVAILABLE (skill not mounted, exec failed, timed out) is
        # not a negative health verdict. Blocking a healthy model because the
        # checker is absent made the proxy less reliable than direct curl —
        # the exact failure issue #14 exists to remove. Proceed, visibly
        # unverified; only an actual DOWN/COLD verdict may block below.
        if health.get("error") in ("ops_chutes_run_not_found", "ops_chutes_exec_failed", "ops_chutes_timeout"):
            plan["action"] = "proceed_unverified"
            plan["preflight_unverified_reason"] = health.get("error")
            return plan
        plan["action"] = "health_check_failed"
        plan["error"] = "ops_chutes_model_health_failed"
        return plan
    if status == "HOT":
        return plan

    recommend = await _run_ops_chutes("recommend", model, "--json", timeout=90.0)
    plan["checks"]["recommend"] = recommend
    rec_json = _ops_stdout_json(recommend) or {}
    plan["recommendation"] = rec_json
    switch_to = rec_json.get("switch_to") or (rec_json.get("best") if rec_json.get("action") == "switch" else None)
    if recommend.get("ok") and isinstance(switch_to, str) and "/" in switch_to:
        plan["model"] = switch_to
        plan["action"] = "switch"
        return plan
    if _OPS_CHUTES_REQUIRE_HOT:
        plan["action"] = "blocked_not_hot"
        plan["error"] = "ops_chutes_model_not_hot"
    return plan


async def _ops_chutes_batch_plan(requests: list[dict]) -> dict[str, Any]:
    """Run budget, feasibility, and per-model ops-chutes checks before a batch."""
    plan: dict[str, Any] = {
        "ok": True,
        "requested_count": len(requests),
        "model_map": {},
        "checks": {},
        "models": {},
    }
    if not _OPS_CHUTES_ENABLED:
        plan["checks"]["ops_chutes"] = {"ok": True, "skipped": True}
        return plan

    budget = await _run_ops_chutes("budget-check", timeout=45.0)
    plan["checks"]["budget_check"] = budget
    if not budget.get("ok"):
        plan.update({"ok": False, "error": "ops_chutes_budget_check_failed"})
        return plan

    feasible = await _run_ops_chutes("can-complete", str(len(requests)), timeout=45.0)
    plan["checks"]["can_complete"] = feasible
    if not feasible.get("ok"):
        plan.update({"ok": False, "error": "ops_chutes_can_complete_failed"})
        return plan

    for model in sorted({str(req.get("model") or "") for req in requests if req.get("model")}):
        model_plan = await _ops_chutes_model_plan(model)
        plan["models"][model] = model_plan
        if model_plan.get("error"):
            plan.update({"ok": False, "error": model_plan["error"]})
            return plan
        plan["model_map"][model] = model_plan.get("model", model)
    return plan


class _AsyncChutesSlot:
    """Acquire the ops-chutes cross-process semaphore without blocking the loop."""

    def __init__(self, timeout: float = 90.0) -> None:
        self.timeout = timeout
        self._sem: Any = None
        self.slot: int | None = None

    async def __aenter__(self) -> "_AsyncChutesSlot":
        if not _OPS_CHUTES_ENABLED or ChutesSemaphore is None:
            return self
        self._sem = ChutesSemaphore(timeout=self.timeout)
        self.slot = await asyncio.to_thread(self._sem.acquire)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()

    async def release(self) -> None:
        if self._sem is not None:
            await asyncio.to_thread(self._sem.release)
            self._sem = None
            self.slot = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0, read=300.0),
        )
    return _http_client


# ---------------------------------------------------------------------------
# Model availability check via Chutes inference API /v1/models
# ---------------------------------------------------------------------------


async def _fetch_available_models() -> list[str]:
    """Fetch available models from Chutes inference API ``/v1/models``.

    Returns list of model IDs (e.g. ``["deepseek-ai/DeepSeek-V3.2-TEE", ...]``).
    Faster and more reliable than the management utilization API.
    """
    if not _CHUTES_API_KEY:
        return []
    client = _get_client()
    try:
        resp = await client.get(
            f"{_CHUTES_INFERENCE_BASE}/v1/models",
            headers={"Authorization": f"Bearer {_CHUTES_API_KEY}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
        logger.warning("chutes_direct: /v1/models returned {}", resp.status_code)
    except Exception as e:
        logger.warning("chutes_direct: /v1/models failed: {}", e)
    return []


_available_models: list[str] | None = None
_models_cache_ts: float = 0.0
_PROMPT_PREFLIGHT_REQUIRED_FIELDS = (
    "full_prompt_payload",
    "expected_result",
    "response_schema",
    "rejection_criteria",
)


async def _get_available_models() -> list[str]:
    """Cached available model list (5 min TTL)."""
    global _available_models, _models_cache_ts
    now = time.monotonic()
    if _available_models is not None and now - _models_cache_ts < _CACHE_TTL_S:
        return _available_models
    models = await _fetch_available_models()
    if models:
        _available_models = models
        _models_cache_ts = now
    return _available_models or []


async def check_model_available(model: str) -> str:
    """Verify model is available on Chutes inference API.

    Returns the requested model ID when an exact live match is found.
    Raises ``MiddlewareReject(503)`` with alternatives if unavailable.
    """
    available = await _get_available_models()
    if not available:
        return model
    if model in available:
        return model
    prefix = model.split("/")[0] + "/" if "/" in model else ""
    candidates = [m for m in available if m.startswith(prefix)] if prefix else []
    from scillm.proxy.middleware import MiddlewareReject
    if candidates:
        raise MiddlewareReject(
            f"Model {model!r} not available on Chutes. "
            f"Available models from same provider: {candidates[:8]}. "
            f"Chutes aliases are disabled; use an exact live model ID from "
            f"``/v1/scillm/chutes/models`` or ``ops-chutes models``.",
            status_code=503,
        )
    raise MiddlewareReject(
        f"Model {model!r} not available on Chutes. "
        f"Chutes aliases are disabled; use an exact live model ID from "
        f"``/v1/scillm/chutes/models`` or ``ops-chutes models``.",
        status_code=503,
    )


async def _resolve_chutes_model_for_call(model: str) -> tuple[str, dict[str, Any] | None]:
    """Require an exact model, then optionally apply ops-chutes hot/cold plan."""
    checked_model = await check_model_available(model)
    if "/" not in checked_model or not _OPS_CHUTES_ENABLED:
        return checked_model, None
    ops_plan = await _ops_chutes_model_plan(checked_model)
    if ops_plan.get("error"):
        from scillm.proxy.middleware import MiddlewareReject
        raise MiddlewareReject(
            f"Chutes model {checked_model!r} failed ops-chutes preflight: "
            f"{ops_plan.get('error')} (health={ops_plan.get('health')})",
            status_code=503,
        )
    planned_model = str(ops_plan.get("model") or checked_model)
    if planned_model != checked_model:
        planned_model = await check_model_available(planned_model)
    return planned_model, ops_plan


# ---------------------------------------------------------------------------
# Direct httpx call (no OpenAI SDK, no middleware)
# ---------------------------------------------------------------------------
# Match json_guard repair by reference
_JSON_REPAIR_IMPORTED = False
try:
    from json_repair import repair_json
    _JSON_REPAIR_IMPORTED = True
except ImportError:
    pass


async def _direct_completion(
    model: str,
    messages: list,
    *,
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    stop: list[str] | str | None = None,
    response_format: dict | None = None,
    seed: int | None = None,
    timeout: float = 300.0,
) -> httpx.Response:
    """Single direct httpx call to Chutes inference API.

    Never passes max_tokens to the provider — reasoning models exhaust
    their output budget on internal thinking when capped.
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if temperature is not None:
        body["temperature"] = temperature
    # max_tokens is intentionally NOT forwarded — reasoning models
    # exhaust their output budget on internal thinking when capped.
    if top_p is not None:
        body["top_p"] = top_p
    if stop is not None:
        body["stop"] = stop
    if response_format is not None:
        body["response_format"] = response_format
    if seed is not None:
        body["seed"] = seed

    client = _get_client()
    if stream:
        slot = _AsyncChutesSlot(timeout=90.0)
        await slot.__aenter__()
        try:
            request = client.build_request(
                "POST",
                f"{_CHUTES_INFERENCE_BASE}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {_CHUTES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp = await client.send(request, stream=True)
            setattr(resp, "_scillm_chutes_slot", slot)
            return resp
        except Exception:
            await slot.release()
            raise

    async with _AsyncChutesSlot(timeout=90.0):
        resp = await client.post(
            f"{_CHUTES_INFERENCE_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_CHUTES_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
    return resp


async def direct_completion(
    model: str,
    messages: list,
    *,
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    stop: list[str] | str | None = None,
    response_format: dict | None = None,
    seed: int | None = None,
    timeout: float = 300.0,
) -> dict:
    """Single completion with availability check + direct call.

    Returns OpenAI-format dict. For streaming, returns
    ``{"_stream_response": resp, "model_served": model}``.

    ``max_tokens`` is silently stripped — reasoning models exhaust their
    output budget on internal thinking when capped.
    """
    best_model, ops_plan = await _resolve_chutes_model_for_call(model)
    resp = await _direct_completion(
        best_model, messages,
        stream=stream,
        temperature=temperature,
        # max_tokens is intentionally stripped
        top_p=top_p,
        stop=stop,
        response_format=response_format,
        seed=seed,
        timeout=timeout,
    )
    if stream:
        resp.raise_for_status()
        return {"_stream_response": resp, "model_served": best_model, "ops_chutes": ops_plan}

    if resp.status_code != 200:
        _raise_for_status(resp, best_model)

    data = resp.json()
    data["model"] = best_model
    if ops_plan is not None:
        data["scillm_chutes"] = {"ops_plan": ops_plan}
    return data


def _raise_for_status(resp: httpx.Response, model: str) -> None:
    """Raise ProxyError or MiddlewareReject from a failed Chutes response."""
    from scillm.proxy.errors import ProxyError
    status = resp.status_code
    try:
        detail = resp.json().get("error", {}).get("message", resp.text[:500])
    except Exception:
        detail = resp.text[:500]
    msg = f"Chutes {model} returned {status}: {detail}"
    if status == 401:
        raise ProxyError(502, f"Chutes auth failed: {detail}", "provider_auth_error")
    if status == 429:
        raise ProxyError(429, f"Chutes rate limited: {detail}", "rate_limit_error")
    if status == 503:
        from scillm.proxy.middleware import MiddlewareReject
        raise MiddlewareReject(f"Chutes unavailable: {detail}", status_code=503)
    raise ProxyError(502, msg, "upstream_error")


# ---------------------------------------------------------------------------
# Batch with semaphore + retry + as_completed
# ---------------------------------------------------------------------------


async def batch_completions(
    requests: list[dict],
    *,
    concurrency: int = 4,
    wall_time_s: float = 600.0,
    progress_heartbeat_s: float | None = None,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 30.0,
    max_retries_5xx: int = 3,
    adaptive_concurrency: bool = True,
    slow_call_s: float = 30.0,
    use_ops_chutes: bool = True,
) -> AsyncIterator[dict]:
    """Batch completions via direct Chutes calls.

    ``requests``: list of per-item dicts. Each may contain:
        model, messages, temperature, max_tokens, top_p, stop, response_format, seed

    Yields ``{index, ok, content/error, model_served, elapsed_s, attempts}``
    as items complete (``asyncio.as_completed`` order, not input order).
    When ``progress_heartbeat_s`` is positive, also yields ``batch_progress``
    heartbeat events while items are pending.

    Semaphore is held only during the HTTP call, not during backoff sleep,
    so retrying one item doesn't block concurrent items from running.
    """
    if not requests:
        return

    batch_plan: dict[str, Any] | None = None
    if use_ops_chutes and any("/" in str(req.get("model") or "") for req in requests):
        batch_plan = await _ops_chutes_batch_plan(requests)
        if not batch_plan.get("ok"):
            yield {
                "type": "batch_preflight",
                "ok": False,
                "error": batch_plan.get("error", "ops_chutes_preflight_failed"),
                "ops_chutes": batch_plan,
            }
            return

    limiter = _AdaptiveConcurrencyLimiter(max(1, concurrency), slow_call_s=slow_call_s)
    sem = asyncio.Semaphore(max(1, concurrency))
    loop = asyncio.get_running_loop()
    start = loop.time()

    def _elapsed():
        return loop.time() - start

    def _should_retry(attempt: int, max_retries: int) -> bool:
        return attempt <= max_retries and _elapsed() < wall_time_s

    def _base_item(idx: int, req: dict) -> dict[str, Any]:
        item: dict[str, Any] = {"index": idx}
        item_id = req.get("item_id") or req.get("id")
        if item_id is not None:
            item["item_id"] = str(item_id)
        return item

    async def _try_call(
        model: str,
        req: dict,
        attempt: int,
        *,
        base_item: dict[str, Any],
        queue: asyncio.Queue[dict[str, Any]] | None,
    ) -> tuple[int, dict | None, str, str | None]:
        """Make one HTTP call attempt. Returns (status_code, data_or_None, model_used, error_or_None)."""
        gate = limiter if adaptive_concurrency else sem
        async with gate:
            call_started = loop.time()
            try:
                best_model = await check_model_available(model)
            except Exception as exc:
                elapsed_s = loop.time() - call_started
                if adaptive_concurrency:
                    limiter.record(status=503, elapsed_s=elapsed_s, attempt=attempt)
                return 503, None, model, str(exc)
            try:
                use_stream = bool(req.get("stream", False))
                resp = await _direct_completion(
                    best_model,
                    req.get("messages", []),
                    stream=use_stream,
                    temperature=req.get("temperature"),
                    # max_tokens intentionally stripped
                    top_p=req.get("top_p"),
                    stop=req.get("stop"),
                    response_format=req.get("response_format"),
                    seed=req.get("seed"),
                    timeout=300.0,
                )
                if use_stream and resp.status_code == 200:
                    data = await _collect_streaming_completion(
                        resp,
                        base_item=base_item,
                        best_model=best_model,
                        attempt=attempt,
                        queue=queue,
                    )
                    elapsed_s = loop.time() - call_started
                    if adaptive_concurrency:
                        limiter.record(status=200, elapsed_s=elapsed_s, attempt=attempt)
                    return 200, data, best_model, None
                if use_stream and resp.status_code != 200:
                    try:
                        await resp.aread()
                    finally:
                        await resp.aclose()
                        slot = getattr(resp, "_scillm_chutes_slot", None)
                        if slot is not None:
                            await slot.release()
            except httpx.TimeoutException:
                elapsed_s = loop.time() - call_started
                if adaptive_concurrency:
                    limiter.record(status=504, elapsed_s=elapsed_s, attempt=attempt)
                return 504, None, best_model, "timeout"
            except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                elapsed_s = loop.time() - call_started
                if adaptive_concurrency:
                    limiter.record(status=502, elapsed_s=elapsed_s, attempt=attempt)
                return 502, None, best_model, str(exc)
            except Exception as exc:
                elapsed_s = loop.time() - call_started
                if adaptive_concurrency:
                    limiter.record(status=0, elapsed_s=elapsed_s, attempt=attempt)
                return 0, None, best_model, str(exc)

            elapsed_s = loop.time() - call_started
            if adaptive_concurrency:
                limiter.record(status=resp.status_code, elapsed_s=elapsed_s, attempt=attempt)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    return 200, data, best_model, None
                except json.JSONDecodeError:
                    return 502, None, best_model, "invalid_json"

            return resp.status_code, None, best_model, resp.text[:300]

    async def _one(idx: int, req: dict) -> dict:
        base_item = _base_item(idx, req)
        model = req.get("model", "")
        if not model:
            return {**base_item, "ok": False, "error": "model_required", "attempts": 0}
        if batch_plan is not None:
            mapped_model = batch_plan.get("model_map", {}).get(model)
            if mapped_model:
                req = {**req, "model": mapped_model}
                base_item["requested_model"] = model
                base_item["ops_chutes"] = batch_plan.get("models", {}).get(model)
                model = mapped_model

        for attempt in range(1, max_retries_5xx + 4):  # +3 for 429 retries
            status, data, best_model, err = await _try_call(
                model,
                req,
                attempt,
                base_item=base_item,
                queue=stream_queue,
            )

            if status == 200 and data is not None:
                content = _extract_content(data)
                return {
                    **base_item,
                    "ok": True,
                    "content": content,
                    "model_served": best_model,
                    "response": data,
                    "attempts": attempt,
                    "elapsed_s": round(_elapsed(), 3),
                    "concurrency_limit": limiter.limit if adaptive_concurrency else concurrency,
                    "concurrency_events": list(limiter.events) if adaptive_concurrency else [],
                }

            if status == 429:
                if _should_retry(attempt, max_retries_5xx + 3):
                    await asyncio.sleep(backoff_cap_s)
                    continue
                return {
                    **base_item,
                    "ok": False,
                    "error": err or "rate_limited",
                    "status": 429,
                    "attempts": attempt,
                    "concurrency_limit": limiter.limit if adaptive_concurrency else concurrency,
                    "concurrency_events": list(limiter.events) if adaptive_concurrency else [],
                }

            if status in (504, 502, 503) and _should_retry(attempt, max_retries_5xx):
                logger.info("chutes_direct: item {} attempt {} got {}, retrying", idx, attempt, status)
                await asyncio.sleep(min(backoff_cap_s, backoff_base * (2 ** (attempt - 1))))
                continue

            return {
                **base_item,
                "ok": False,
                "error": err or f"http_{status}",
                "status": status,
                "attempts": attempt,
                "concurrency_limit": limiter.limit if adaptive_concurrency else concurrency,
                "concurrency_events": list(limiter.events) if adaptive_concurrency else [],
            }

        return {
            **base_item,
            "ok": False,
            "error": "max_retries_exceeded",
            "status": None,
            "attempts": max_retries_5xx + 3,
            "concurrency_limit": limiter.limit if adaptive_concurrency else concurrency,
            "concurrency_events": list(limiter.events) if adaptive_concurrency else [],
        }

    stream_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task_items = [
        (loop.create_task(_one(i, r or {})), i, r or {})
        for i, r in enumerate(requests)
    ]
    pending = {task for task, _, _ in task_items}
    task_meta = {task: (idx, req) for task, idx, req in task_items}
    deadline = start + max(1.0, wall_time_s)

    while pending:
        remaining_s = deadline - loop.time()
        if remaining_s <= 0:
            break
        done, pending = await asyncio.wait(
            pending,
            timeout=min(remaining_s, progress_heartbeat_s) if progress_heartbeat_s and progress_heartbeat_s > 0 else remaining_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            while not stream_queue.empty():
                yield stream_queue.get_nowait()
            if progress_heartbeat_s and progress_heartbeat_s > 0 and pending:
                pending_items = []
                for task in sorted(pending, key=lambda item: task_meta[item][0]):
                    idx, req = task_meta[task]
                    pending_items.append(_base_item(idx, req))
                yield {
                    "type": "batch_progress",
                    "ok": True,
                    "pending_count": len(pending),
                    "pending_items": pending_items,
                    "elapsed_s": round(_elapsed(), 3),
                    "concurrency_limit": limiter.limit if adaptive_concurrency else concurrency,
                    "concurrency_events": list(limiter.events) if adaptive_concurrency else [],
                }
                continue
            break
        while not stream_queue.empty():
            yield stream_queue.get_nowait()
        for task in done:
            while not stream_queue.empty():
                yield stream_queue.get_nowait()
            yield await task
            while not stream_queue.empty():
                yield stream_queue.get_nowait()

    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in sorted(pending, key=lambda item: task_meta[item][0]):
            idx, req = task_meta[task]
            yield {
                **_base_item(idx, req),
                "ok": False,
                "error": "wall_time_exceeded",
                "status": 504,
                "attempts": None,
                "elapsed_s": round(_elapsed(), 3),
                "concurrency_limit": limiter.limit if adaptive_concurrency else concurrency,
                "concurrency_events": list(limiter.events) if adaptive_concurrency else [],
            }


def _extract_first_json_object(text: str | None) -> dict[str, Any] | None:
    """Extract the first JSON object from model text that may include prose."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _validate_prompt_preflight_packet(packet: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in _PROMPT_PREFLIGHT_REQUIRED_FIELDS:
        value = packet.get(field)
        if value is None or value == "":
            missing.append(field)
    return missing


def _validate_full_prompt_payload(payload: Any) -> list[str]:
    missing: list[str] = []
    if not isinstance(payload, dict):
        return ["full_prompt_payload"]
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        missing.append("full_prompt_payload.messages")
    return missing


def _final_batch_item_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the terminal batch item event, ignoring live delta/progress events."""
    for event in reversed(events):
        if event.get("type") in {
            "target_content_delta",
            "target_reasoning_delta",
            "batch_progress",
            "batch_preflight",
        }:
            continue
        return event
    return events[-1] if events else {}


async def run_full_prompt_payload_probe(
    packet: dict[str, Any],
    *,
    model: str,
    response_format: dict | None = None,
    wall_time_s: float = 300.0,
) -> dict[str, Any]:
    """Run one real Chutes call with the representative full prompt payload.

    This is a transport/schema viability gate for structured batches. It checks
    the exact rendered prompt payload before target batch spend. Prompt-reviewer
    can judge wording, but this probe proves the provider can complete the
    full payload and that the response has the expected JSON shape.
    """
    if not isinstance(packet, dict):
        return {
            "ok": False,
            "error": "prompt_preflight_must_be_object",
            "probe_transport": "scillm_chutes_batch",
        }
    payload = packet.get("full_prompt_payload")
    missing = _validate_full_prompt_payload(payload)
    if missing:
        return {
            "ok": False,
            "error": "full_prompt_payload_missing_required_fields",
            "missing": missing,
            "probe_transport": "scillm_chutes_batch",
        }
    assert isinstance(payload, dict)
    probe_model = str(payload.get("model") or model or "")
    if not probe_model:
        return {
            "ok": False,
            "error": "full_prompt_probe_model_required",
            "probe_transport": "scillm_chutes_batch",
        }
    probe_response_format = payload.get("response_format") or response_format
    probe_request = {
        "item_id": "prompt-full-payload-probe",
        "model": probe_model,
        "messages": payload["messages"],
        "response_format": probe_response_format,
    }
    for optional_key in ("temperature", "top_p", "stop", "seed", "stream"):
        if optional_key in payload:
            probe_request[optional_key] = payload[optional_key]

    probe_events = [
        event
        async for event in batch_completions(
            [probe_request],
            concurrency=1,
            wall_time_s=wall_time_s,
        )
    ]
    event = _final_batch_item_event(probe_events)
    if not event.get("ok"):
        return {
            "ok": False,
            "error": event.get("error") or "full_prompt_payload_probe_failed",
            "probe_transport": "scillm_chutes_batch",
            "probe_event": event,
        }

    expected_result = packet.get("expected_result")
    parsed = _extract_first_json_object(event.get("content"))
    if isinstance(expected_result, dict):
        if not isinstance(parsed, dict):
            return {
                "ok": False,
                "error": "full_prompt_payload_probe_non_json_object",
                "probe_transport": "scillm_chutes_batch",
                "probe_event": event,
            }
        missing_keys = sorted(set(expected_result) - set(parsed))
        if missing_keys:
            return {
                "ok": False,
                "error": "full_prompt_payload_probe_missing_expected_keys",
                "missing": missing_keys,
                "probe_transport": "scillm_chutes_batch",
                "probe_event": event,
                "parsed": parsed,
            }
    return {
        "ok": True,
        "probe_transport": "scillm_chutes_batch",
        "probe_model": probe_model,
        "probe_event": event,
        "parsed": parsed,
    }


def _build_prompt_preflight_review_messages(packet: dict[str, Any]) -> list[dict[str, str]]:
    """Build the prompt-reviewer request for one representative batch payload."""
    review_payload = {
        "task": "review_batch_prompt_before_chutes_batch_launch",
        "instructions": [
            "Review the full prompt payload against the expected result, schema, and rejection criteria.",
            "Return JSON only.",
            "If the prompt is vague, under-specified, or likely to miss required fields, set prompt_review.status to NEEDS_CHANGES.",
            "If the prompt is usable for the batch, set prompt_review.status to PASS.",
            "Do not rewrite or echo the full prompt. Set fixed_prompt_template to an empty string unless a concise replacement under 2000 characters is essential.",
            "Put concise actionable findings in prompt_review.reasons or prompt_review.findings.",
        ],
        "full_prompt_payload": packet.get("full_prompt_payload"),
        "expected_result": packet.get("expected_result"),
        "response_schema": packet.get("response_schema"),
        "rejection_criteria": packet.get("rejection_criteria"),
        "validation_command": packet.get("validation_command"),
        "consumer": packet.get("consumer"),
        "batch_context": packet.get("batch_context"),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are prompt-reviewer. Return strict JSON with keys "
                "prompt_review and fixed_prompt_template. Do not include prose."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(review_payload, indent=2, sort_keys=True),
        },
    ]


async def run_prompt_preflight(
    packet: dict[str, Any],
    *,
    reviewer_model: str,
    wall_time_s: float = 300.0,
) -> dict[str, Any]:
    """Run a Chutes-backed prompt-reviewer preflight before a target batch."""
    if not isinstance(packet, dict):
        return {
            "ok": False,
            "error": "prompt_preflight_must_be_object",
            "reviewer_transport": "scillm_chutes_batch",
        }
    missing = _validate_prompt_preflight_packet(packet)
    if missing:
        return {
            "ok": False,
            "error": "prompt_preflight_missing_required_fields",
            "missing": missing,
            "reviewer_transport": "scillm_chutes_batch",
        }
    if not reviewer_model:
        return {
            "ok": False,
            "error": "prompt_reviewer_model_required",
            "reviewer_transport": "scillm_chutes_batch",
        }

    review_events = [
        event
        async for event in batch_completions(
            [
                {
                    "item_id": "prompt-reviewer-preflight",
                    "model": reviewer_model,
                    "messages": _build_prompt_preflight_review_messages(packet),
                    "response_format": {"type": "json_object"},
                    "stream": True,
                }
            ],
            concurrency=1,
            wall_time_s=wall_time_s,
        )
    ]
    event = _final_batch_item_event(review_events)
    if not event.get("ok"):
        return {
            "ok": False,
            "error": event.get("error") or "prompt_reviewer_batch_failed",
            "reviewer_transport": "scillm_chutes_batch",
            "reviewer_event": event,
        }

    artifact = _extract_first_json_object(event.get("content"))
    prompt_review = artifact.get("prompt_review") if isinstance(artifact, dict) else None
    fixed_template = artifact.get("fixed_prompt_template") if isinstance(artifact, dict) else ""
    if not isinstance(prompt_review, dict):
        return {
            "ok": False,
            "error": "prompt_reviewer_artifact_invalid",
            "reviewer_transport": "scillm_chutes_batch",
            "reviewer_event": event,
            "artifact": artifact,
        }
    if not isinstance(fixed_template, str):
        artifact["fixed_prompt_template"] = ""
    status = str(prompt_review.get("status") or "").upper()
    if status in {"FAIL", "FAILED", "REJECT", "REJECTED", "NEEDS_CHANGES"}:
        return {
            "ok": False,
            "error": "prompt_reviewer_rejected_prompt",
            "reviewer_transport": "scillm_chutes_batch",
            "reviewer_event": event,
            "artifact": artifact,
        }
    return {
        "ok": True,
        "reviewer_transport": "scillm_chutes_batch",
        "reviewer_model": reviewer_model,
        "artifact": artifact,
        "reviewer_event": event,
    }


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return None


def _extract_content(data: dict) -> str | None:
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        return msg.get("content")
    return None
