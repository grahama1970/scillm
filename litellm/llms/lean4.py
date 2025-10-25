from __future__ import annotations

"""
Experimental env-gated provider: lean4

Intent
- Let Router users call Lean4 via a familiar LiteLLM provider surface.
- Posts to the Lean4 bridge `/bridge/complete` and returns a ModelResponse with
  a concise textual summary; attaches full results in `additional_kwargs['lean4']`.

Enable with: LITELLM_ENABLE_LEAN4=1
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Union, Callable, Dict, List

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse


def _resolve_base(api_base: Optional[str]) -> str:
    base = (
        api_base
        or os.getenv("CERTAINLY_BRIDGE_BASE")
        or os.getenv("LEAN4_BRIDGE_BASE")
        or "http://127.0.0.1:8787"
    )
    return base.rstrip("/")


def _ping_healthz(base: str, timeout_s: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.get(f"{base}/healthz")
            return 200 <= r.status_code < 300 and (r.json() or {}).get("ok") is True
    except Exception:
        return False


def _autostart_bridge_if_needed(base_hint: str | None = None) -> str:
    """Ensure a local bridge is up. Best-effort, no-throw.

    Policy:
    - If CERTAINLY_BRIDGE_BASE/LEAN4_BRIDGE_BASE is set, do not mutate; just return it.
    - If not set and SCILLM_AUTO_START_BRIDGES!=1, return provided hint or default.
    - If enabled, start container via docker compose file under this repo and wait for health.
    - Uses host port 8791 (container 8787) to avoid conflicts.
    """
    env_base = os.getenv("CERTAINLY_BRIDGE_BASE") or os.getenv("LEAN4_BRIDGE_BASE")
    # Decide whether auto-start is enabled: ON by default for localhost, else env-gated
    def _is_local(url: str | None) -> bool:
        if not url:
            return False
        u = str(url)
        return u.startswith("http://127.0.0.1:") or u.startswith("http://localhost:")

    desired = (env_base or base_hint or "http://127.0.0.1:8787").rstrip("/")
    auto_flag = os.getenv("SCILLM_AUTO_START_BRIDGES")
    auto_enabled = (_is_local(desired) and auto_flag != "0") or (auto_flag == "1")
    if not auto_enabled:
        return desired

    # Compose file path relative to repo root + simple TTL so we don't spam docker
    root = Path(__file__).resolve().parents[2]
    compose = root / "docker" / "compose.certainly.bridge.yml"
    target = "http://127.0.0.1:8791"
    ttl_flag = root / ".artifacts" / "autostart_ttl_lean4"
    try:
        if not _ping_healthz(target, timeout_s=1.5):
            # 1) If a container exists but is unhealthy, try a fast restart
            try:
                # Prefer scoped name with project, but also try legacy name
                for name in ("scillm-bridges-certainly_bridge-1", "certainly_bridge"):
                    subprocess.run(["docker","restart", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # short grace
                for _ in range(5):
                    if _ping_healthz(target, timeout_s=1.0):
                        return target
                    time.sleep(0.6)
            except Exception:
                pass
            # honor TTL of ~30s
            try:
                ttl_flag.parent.mkdir(parents=True, exist_ok=True)
                import time as _t
                if ttl_flag.exists() and (int(_t.time()) - int(ttl_flag.read_text().strip() or 0)) < 30:
                    return desired
                ttl_flag.write_text(str(int(_t.time())))
            except Exception:
                pass
            env = os.environ.copy()
            env.setdefault("COMPOSE_PROJECT_NAME", "scillm-bridges")
            subprocess.run([
                "docker", "compose", "-f", str(compose), "up", "-d"
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            # wait briefly for health
            for _ in range(10):
                if _ping_healthz(target, timeout_s=1.5):
                    break
                time.sleep(0.6)
    except Exception:
        # silent best-effort; fall back to hint/default
        return (base_hint or "http://127.0.0.1:8787").rstrip("/")
    return target


def _shape_payload(messages: list, optional_params: dict | None) -> Dict[str, Any]:
    opt = dict(optional_params or {})
    # Optional multi-prover hint (placeholder): backend = "lean4" | "coq"
    backend = (opt.pop("backend", None) or os.getenv("CERTAINLY_BACKEND") or "lean4").lower()
    # Accept either 'lean4_requirements' or generic 'items'
    requirements = opt.pop("lean4_requirements", None) or opt.pop("items", None)
    if not isinstance(requirements, list) or not requirements:
        raise CustomLLMError(status_code=400, message="lean4 provider requires 'lean4_requirements' or 'items' list")
    flags = opt.pop("lean4_flags", None) or opt.pop("flags", None)
    # If no flags provided and CERTAINLY_LLM_DEFAULT=1, inject sane LLM defaults
    if not flags:
        try:
            llm_default = (os.getenv("CERTAINLY_LLM_DEFAULT") or "").strip().lower() in {"1", "true", "yes", "on"}
            if llm_default:
                model_env = (os.getenv("CERTAINLY_MODEL") or os.getenv("OPENROUTER_DEFAULT_MODEL") or "").strip()
                flags = [
                    "--model", model_env or "",
                    "--strategies", "direct,structured,computational",
                    "--max-refinements", "1",
                    "--best-of",
                ]
        except Exception:
            pass
    payload: Dict[str, Any] = {"messages": messages, "lean4_requirements": requirements, "backend": backend}
    if isinstance(flags, list) and flags:
        payload["lean4_flags"] = flags
    # Pass through max_seconds if provided
    if "max_seconds" in opt:
        try:
            payload["max_seconds"] = float(opt.pop("max_seconds"))
        except Exception:
            pass
    # Forward options.session_id/track_id for parity with CodeWorld manifests
    options = {}
    try:
        if isinstance(opt.get("options"), dict):
            options.update(opt.pop("options"))
    except Exception:
        pass
    session_id = opt.pop("session_id", None)
    track_id = opt.pop("track_id", None)
    if session_id is not None:
        options["session_id"] = session_id
    if track_id is not None:
        options["track_id"] = track_id
    if options:
        payload["options"] = options
    return payload


def _summarize(resp_json: Dict[str, Any], label: str = "Lean4") -> str:
    try:
        s = resp_json.get("summary", {}) or {}
        items = s.get("items")
        proved = s.get("proved")
        failed = s.get("failed")
        unproved = s.get("unproved")
        return f"{label}: items={items}, proved={proved}, failed={failed}, unproved={unproved}"
    except Exception:
        return f"{label}: completed (see additional_kwargs.certainly)"


class Lean4LLM(CustomLLM):
    def completion(
        self,
        model: str,
        messages: list,
        api_base: Optional[str],
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: dict = {},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[HTTPHandler] = None,
    ) -> ModelResponse:
        base = _resolve_base(api_base)
        # Optional autostart for local bridges (env-gated)
        base = _autostart_bridge_if_needed(base)
        payload = _shape_payload(messages, optional_params)
        req_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 60.0
        try:
            if isinstance(client, HTTPHandler):
                r = client.post(f"{base}/bridge/complete", json=payload, headers=headers or None, timeout=req_timeout)
                if getattr(r, "status_code", 200) < 200 or getattr(r, "status_code", 200) >= 300:
                    # some injected handlers may not raise; normalize here
                    body = getattr(r, "text", "")
                    raise CustomLLMError(status_code=getattr(r, "status_code", 500), message=str(body)[:400])
            else:
                with httpx.Client(timeout=req_timeout, headers=headers) as c:
                    r = c.post(f"{base}/bridge/complete", json=payload)
                    if r.status_code < 200 or r.status_code >= 300:
                        raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
            data = r.json()
        except CustomLLMError:
            raise
        except Exception as e:  # pragma: no cover
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        backend_label = payload.get("backend") or "lean4"
        label = "Certainly" if backend_label == "lean4" else f"Certainly/{backend_label}"
        text = _summarize(data if isinstance(data, dict) else {}, label=label)
        model_response.model = model
        try:
            model_response.choices[0].message.content = text  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": text}  # type: ignore[assignment]
        # Attach full payload
        try:
            model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
            attach_both = os.getenv("LITELLM_CERTAINLY_ATTACH_BOTH", "1") == "1"
            # Always attach under 'certainly'
            model_response.additional_kwargs["certainly"] = data
            if attach_both:
                model_response.additional_kwargs["lean4"] = data
        except Exception:
            pass
        return model_response

    async def acompletion(
        self,
        model: str,
        messages: list,
        api_base: Optional[str],
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: dict = {},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[AsyncHTTPHandler] = None,
    ) -> ModelResponse:
        base = _resolve_base(api_base)
        base = _autostart_bridge_if_needed(base)
        payload = _shape_payload(messages, optional_params)
        req_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 60.0
        try:
            if isinstance(client, AsyncHTTPHandler):
                r = await client.post(f"{base}/bridge/complete", json=payload, headers=headers or None, timeout=req_timeout)
                if getattr(r, "status_code", 200) < 200 or getattr(r, "status_code", 200) >= 300:
                    body = getattr(r, "text", "")
                    raise CustomLLMError(status_code=getattr(r, "status_code", 500), message=str(body)[:400])
            else:
                async with httpx.AsyncClient(timeout=req_timeout, headers=headers) as c:
                    r = await c.post(f"{base}/bridge/complete", json=payload)
                    if r.status_code < 200 or r.status_code >= 300:
                        raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
            data = r.json()
        except CustomLLMError:
            raise
        except Exception as e:  # pragma: no cover
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        backend_label = payload.get("backend") or "lean4"
        label = "Certainly" if backend_label == "lean4" else f"Certainly/{backend_label}"
        text = _summarize(data if isinstance(data, dict) else {}, label=label)
        model_response.model = model
        try:
            model_response.choices[0].message.content = text  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": text}  # type: ignore[assignment]
        try:
            model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
            attach_both = os.getenv("LITELLM_CERTAINLY_ATTACH_BOTH", "1") == "1"
            model_response.additional_kwargs["certainly"] = data
            if attach_both:
                model_response.additional_kwargs["lean4"] = data
        except Exception:
            pass
        return model_response


# --- Optional self-registration (env-gated) -----------------------------------
try:
    if os.getenv("LITELLM_ENABLE_LEAN4", "") == "1":
        try:
            from litellm.llms import PROVIDER_REGISTRY  # type: ignore
            PROVIDER_REGISTRY["lean4"] = Lean4LLM
        except Exception:
            try:
                from litellm.llms.custom_llm import register_custom_provider  # type: ignore
                register_custom_provider("lean4", Lean4LLM)
            except Exception:
                pass
except Exception:
    pass
