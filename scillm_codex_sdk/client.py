# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Dict, List, Optional, TypedDict, Tuple

import httpx
from .auth import build_auth_headers, get_bearer_token, ensure_token_or_warn
from .http import normalize_base_url, make_async_client, SERVICE_PREFIX as _P, request_json
from .errors import CodexHttpError, CodexAuthError, CodexCloudError


class CreateTaskResult(TypedDict):
    id: str


class TaskSummary(TypedDict, total=False):
    id: str
    title: str
    status: str
    updated_at: str
    created_at: Optional[str]
    environment_id: Optional[str]
    environment_label: Optional[str]
    turn_status: Optional[str]


class CloudTasksClient:
    """
    Minimal Cloud Tasks client for Codex Cloud.
    Endpoints (configurable prefix, default '/wham'):
      - GET  {prefix}/environments
      - POST {prefix}/tasks
      - GET  {prefix}/tasks/{task_id}
      - GET  {prefix}/tasks/{task_id}/text
      - GET  {prefix}/tasks/{task_id}/diff
      - GET  {prefix}/tasks/{task_id}/messages
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        user_agent: str = "scillm-codex-sdk/0.1",
        timeout_s: float = 60.0,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.headers = build_auth_headers(user_agent=user_agent)
        if "Authorization" not in self.headers:
            ensure_token_or_warn(None)
        self.timeout_s = timeout_s
        self.use_camel = os.getenv("CODEX_CLOUD_CAMEL", "").lower() in {"1", "true", "yes"}

    async def _req_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        max_attempts: int = 6,
        budget_s: float = 60.0,
    ) -> httpx.Response:
        start = time.time()
        attempt = 0
        backoff = 0.5
        while True:
            try:
                if method == "GET":
                    r = await client.get(url)
                else:
                    r = await client.post(url, json=json_body)
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    try:
                        sleep_s = max(0.5, float(ra)) if ra else backoff
                    except Exception:
                        sleep_s = backoff
                    if (time.time() - start + sleep_s) > budget_s or attempt >= max_attempts:
                        r.raise_for_status()
                    await asyncio.sleep(sleep_s + random.random() * 0.25)
                    backoff = min(backoff * 2, 10.0)
                    attempt += 1
                    continue
                if r.status_code >= 500 and (attempt < max_attempts and (time.time() - start) < budget_s):
                    await asyncio.sleep(backoff + random.random() * 0.25)
                    backoff = min(backoff * 2, 10.0)
                    attempt += 1
                    continue
                return r
            except httpx.TransportError:
                if attempt >= max_attempts or (time.time() - start) >= budget_s:
                    raise
                await asyncio.sleep(backoff + random.random() * 0.25)
                backoff = min(backoff * 2, 10.0)
                attempt += 1

    async def _get(self, client: httpx.AsyncClient, path: str) -> httpx.Response:
        r = await self._req_with_retry(client, "GET", f"{self.base_url}{path}")
        if r.status_code >= 400:
            rid = r.headers.get("x-request-id") or r.headers.get("request-id")
            raise CodexHttpError("GET failed", status=r.status_code, details={"path": path, "request_id": rid, "text": r.text[:500]})
        return r

    async def _post(self, client: httpx.AsyncClient, path: str, json_body: Dict[str, Any]) -> httpx.Response:
        r = await self._req_with_retry(client, "POST", f"{self.base_url}{path}", json_body=json_body)
        if r.status_code >= 400:
            rid = r.headers.get("x-request-id") or r.headers.get("request-id")
            raise CodexHttpError("POST failed", status=r.status_code, details={"path": path, "request_id": rid, "text": r.text[:500]})
        return r

    async def list_environments(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        resp = await self._get(client, f"{_P}/environments")
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("data") or data

    async def resolve_environment_id(self, client: httpx.AsyncClient, label: str) -> str:
        envs = await self.list_environments(client)
        for e in envs:
            if str(e.get("label", "")) == label:
                return str(e.get("id"))
        for e in envs:
            if str(e.get("label", "")).lower() == label.lower():
                return str(e.get("id"))
        for e in envs:
            if str(e.get("id")) == label:
                return str(e.get("id"))
        raise CodexHttpError(f"Environment not found for label '{label}'", status=404)

    async def create_task(
        self,
        client: httpx.AsyncClient,
        environment_id: str,
        prompt: str,
        git_ref: str = "main",
        qa_mode: Optional[bool] = None,
        best_of_n: Optional[int] = None,
    ) -> CreateTaskResult:
        if self.use_camel:
            payload: Dict[str, Any] = {"environmentId": environment_id, "prompt": prompt, "gitRef": git_ref}
            if qa_mode is not None:
                payload["qaMode"] = bool(qa_mode)
            if best_of_n is not None:
                payload["bestOfN"] = int(best_of_n)
        else:
            payload = {"environment_id": environment_id, "prompt": prompt, "git_ref": git_ref}
            if qa_mode is not None:
                payload["qa_mode"] = bool(qa_mode)
            if best_of_n is not None:
                payload["best_of_n"] = int(best_of_n)
        resp = await self._post(client, f"{_P}/tasks", payload)
        data = resp.json()
        if isinstance(data, dict) and "id" in data:
            id_val = data["id"]
            if isinstance(id_val, str):
                return {"id": id_val}
            if isinstance(id_val, dict) and "0" in id_val:
                return {"id": str(id_val["0"])}
        task_id = str(data.get("id") or data.get("task_id") or "").strip()
        if task_id:
            return {"id": task_id}
        raise CodexHttpError("Task creation response missing 'id'", status=502, details=data)

    async def get_task(self, client: httpx.AsyncClient, task_id: str) -> TaskSummary:
        resp = await self._get(client, f"{_P}/tasks/{task_id}")
        data = resp.json()
        if isinstance(data, dict):
            return data
        raise CodexHttpError("Unexpected task shape", status=502, details=data)

    async def get_task_text(self, client: httpx.AsyncClient, task_id: str) -> Dict[str, Any]:
        resp = await self._get(client, f"{_P}/tasks/{task_id}/text")
        data = resp.json()
        if isinstance(data, dict):
            return data
        raise CodexHttpError("Unexpected task text shape", status=502, details=data)

    async def get_task_messages(self, client: httpx.AsyncClient, task_id: str) -> List[str]:
        resp = await self._get(client, f"{_P}/tasks/{task_id}/messages")
        data = resp.json()
        if isinstance(data, list):
            return [str(x) for x in data]
        maybe = data.get("messages") if isinstance(data, dict) else None
        if isinstance(maybe, list):
            return [str(x) for x in maybe]
        raise CodexHttpError("Unexpected task messages shape", status=502, details=data)

    async def get_task_diff(self, client: httpx.AsyncClient, task_id: str) -> Optional[str]:
        r = await self._req_with_retry(client, "GET", f"{self.base_url}{_P}/tasks/{task_id}/diff")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise CodexHttpError("GET /diff failed", status=r.status_code, details=r.text[:500])
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                data = r.json()
                if isinstance(data, dict) and "diff" in data:
                    return str(data["diff"])
            except Exception:
                pass
        return r.text

    async def chat_to_task(
        self,
        messages: List[Dict[str, Any]],
        env_label: Optional[str] = None,
        env_id: Optional[str] = None,
        timeout_s: int = 300,
        git_ref: str = "main",
        qa_mode: Optional[bool] = None,
        best_of_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        user_msgs = [
            m.get("content", "").strip()
            for m in messages
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        prompt = "\n\n".join([m for m in user_msgs if m]) or "Generate a concise plan and minimal diff."

        use_env_id = env_id
        if not use_env_id:
            env_label = env_label or os.getenv("ENV_LABEL")
            if not env_label:
                raise ValueError("Missing env_label or ENV_LABEL; or pass env_id.")

        async with make_async_client(self.headers, timeout=self.timeout_s) as http:
            if not use_env_id:
                use_env_id = await self.resolve_environment_id(http, env_label)

            created = await self.create_task(http, use_env_id, prompt, git_ref=git_ref, qa_mode=qa_mode, best_of_n=best_of_n)
            task_id = created["id"]

            start = time.time()
            interval = 1.5
            while True:
                task = await self.get_task(http, task_id)
                status = (task.get("status") or "").lower()
                turn = (task.get("turn_status") or task.get("turnStatus") or "").lower()
                if status in ("ready", "error") and (not turn or turn in ("completed", "failed")):
                    break
                if time.time() - start > timeout_s:
                    raise TimeoutError("Timeout waiting for task completion")
                await asyncio.sleep(interval)
                interval = min(interval * 1.2, 5.0)

            text = await self.get_task_text(http, task_id)
            diff = await self.get_task_diff(http, task_id)
            msgs = await self.get_task_messages(http, task_id)

        content_lines = [f"Task {task_id} — status: {task.get('status', 'unknown')}"]
        if text.get("prompt"):
            content_lines.append("\nPrompt:\n" + str(text["prompt"]))
        if msgs:
            content_lines.append("\nFirst message:\n" + str(msgs[0])[:800])
        if diff:
            content_lines.append(f"\nDiff available ({len(diff.splitlines())} lines).")
        else:
            content_lines.append("\nNo diff available.")
        return {
            "id": task_id,
            "status": task.get("status"),
            "turn_status": task.get("turn_status") or task.get("turnStatus"),
            "content": "\n".join(content_lines),
        }


class CloudTasksClientSync:
    """Very thin sync wrapper for convenience in sync code paths."""

    def __init__(self, **kwargs: Any) -> None:
        self._c = CloudTasksClient(**kwargs)

    def chat_to_task(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return asyncio.run(self._c.chat_to_task(*args, **kwargs))


# Additional simple client surface (non-async) as requested
class CodexCloudClient:
    """
    Simple HTTP client with resilient retry/backoff and parameter-first helpers.
    Honors env: CODEX_CLOUD_TASKS_BASE_URL, CODEX_CLOUD_SERVICE_PREFIX, CODEX_CLOUD_CAMEL,
    CODEX_CLOUD_TOKEN (or ~/.codex/auth.json), CODEX_CLOUD_ECHO.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        service_prefix: Optional[str] = None,
        camel_outgoing: Optional[bool] = None,
        timeout_s: float = 30.0,
        retry_time_budget_s: float = 60.0,
        retry_max_attempts: int = 8,
        retry_base_s: float = 1.5,
        retry_cap_s: float = 90.0,
        retry_jitter_pct: float = 0.25,
        honor_retry_after: bool = True,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.prefix = os.getenv("CODEX_CLOUD_SERVICE_PREFIX") or service_prefix or ""
        if self.prefix and not self.prefix.startswith("/"):
            self.prefix = "/" + self.prefix
        self.camel = camel_outgoing if camel_outgoing is not None else (os.getenv("CODEX_CLOUD_CAMEL") in {"1", "true", "True"})
        self.timeout_s = timeout_s
        self.retry_params: Tuple[float, int, float, float, float, bool] = (
            retry_time_budget_s,
            retry_max_attempts,
            retry_base_s,
            retry_cap_s,
            retry_jitter_pct,
            honor_retry_after,
        )
        self.echo = os.getenv("CODEX_CLOUD_ECHO") in {"1", "true", "True"}
        self.token = get_bearer_token(token)
        ensure_token_or_warn(self.token)

    def _headers(self) -> Dict[str, str]:
        h = {"accept": "application/json", "content-type": "application/json"}
        if self.token:
            h["authorization"] = f"Bearer {self.token}"
        return h

    def _join(self, path: str) -> str:
        if not self.base_url:
            raise CodexCloudError("Missing CODEX_CLOUD_TASKS_BASE_URL")
        return f"{self.base_url}{self.prefix}{path}"

    def _post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, str], Any, Dict[str, Any]]:
        if self.camel:
            def _cam(o: Any) -> Any:
                if isinstance(o, dict):
                    return {self._to_camel(k): _cam(v) for k, v in o.items()}
                if isinstance(o, list):
                    return [_cam(x) for x in o]
                return o
            body = _cam(body)
        rt = request_json(
            "POST",
            self._join(path),
            headers=self._headers(),
            json_body=body,
            timeout_s=self.timeout_s,
            retry_time_budget_s=self.retry_params[0],
            retry_max_attempts=self.retry_params[1],
            retry_base_s=self.retry_params[2],
            retry_cap_s=self.retry_params[3],
            retry_jitter_pct=self.retry_params[4],
            honor_retry_after=self.retry_params[5],
        )
        return rt

    @staticmethod
    def _to_camel(s: str) -> str:
        parts = s.split("_")
        return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])

    def chat(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.echo:
            content = f"echo:{messages[-1].get('content') if messages else 'ok'}"
            return {
                "id": f"chatcmpl-{int(time.time()*1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model or "codex-cloud/echo",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "additional_kwargs": {"codex_cloud": {"request_id": "echo", "retries": {"attempts": 1, "total_sleep_s": 0.0}}},
            }
        body: Dict[str, Any] = {"messages": messages}
        if model is not None:
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if reasoning is not None:
            body["reasoning"] = reasoning
        if extra:
            body.update(extra)
        last_err: Optional[Exception] = None
        for p in ("/v1/tasks/chat", "/v1/chat", "/chat", "/v1/chat/completions"):
            try:
                status, headers, data, meta = self._post(p, body)
                rid = headers.get("x-request-id") or headers.get("request-id") or meta.get("request_id")
                if isinstance(data, dict) and "choices" in data:
                    ak = (data.setdefault("additional_kwargs", {})).setdefault("codex_cloud", {})
                    ak.setdefault("request_id", rid)
                    ak.setdefault("retries", {k: meta.get(k) for k in ("attempts", "total_sleep_s", "last_retry_after_s")})
                    return data
                content = None
                if isinstance(data, dict):
                    content = data.get("content") or data.get("text") or data.get("answer")
                content = content or "OK"
                return {
                    "id": f"chatcmpl-{int(time.time()*1000)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model or "codex-cloud/unknown",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": str(content)}}],
                    "additional_kwargs": {"codex_cloud": {"request_id": rid, "retries": {k: meta.get(k) for k in ("attempts", "total_sleep_s", "last_retry_after_s")}}},
                }
            except Exception as e:
                last_err = e
                continue
        raise CodexCloudError(f"All chat endpoints failed: {last_err}")

    def generate_variants(
        self,
        *,
        prompt: str,
        n: int = 3,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.echo:
            now = int(time.time() * 1000)
            out = [{"id": f"var-{i+1}-{now}", "content": f"{prompt} [v{i+1}]"} for i in range(max(1, int(n)))]
            return {"variants": out, "request_id": "echo"}
        body: Dict[str, Any] = {"prompt": prompt, "n": n}
        if model is not None:
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        if extra:
            body.update(extra)
        last_err: Optional[Exception] = None
        for p in ("/v1/tasks/variants", "/v1/variants", "/variants"):
            try:
                status, headers, data, meta = self._post(p, body)
                rid = headers.get("x-request-id") or headers.get("request-id") or meta.get("request_id")
                if isinstance(data, dict) and isinstance(data.get("variants"), list):
                    return {"variants": data["variants"], "request_id": rid}
                if isinstance(data, list):
                    norm = []
                    for i, v in enumerate(data):
                        if isinstance(v, dict):
                            cid = v.get("id") or f"var-{i+1}"
                            ccontent = v.get("content") or v.get("text") or ""
                        else:
                            cid = f"var-{i+1}"
                            ccontent = str(v)
                        norm.append({"id": cid, "content": ccontent})
                    return {"variants": norm, "request_id": rid}
                return {"variants": [{"id": "var-1", "content": str(data)}], "request_id": rid}
            except Exception as e:
                last_err = e
                continue
        raise CodexCloudError(f"All variants endpoints failed: {last_err}")
