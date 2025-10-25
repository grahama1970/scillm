from __future__ import annotations

"""
Custom provider: codeworld

Bridges LiteLLM calls to the CodeWorld Bridge API.
- POST /bridge/complete for bounded sync runs (flips to async if budget > threshold)
- On HTTP 202, provider polls /bridge/result/{job_id} until completion or budget expiry

Usage via Router model_list:
    Router(model_list=[{
        "model_name": "codeworld",
        "litellm_params": {
            "model": "codeworld",
            "custom_llm_provider": "codeworld",
            # Optional (or env):
            # "api_base": "http://127.0.0.1:8000",
            # "api_key": os.getenv("CODEWORLD_TOKEN"),
        }
    }])

Env fallbacks:
- CODEWORLD_BASE  (default http://127.0.0.1:8000)
- CODEWORLD_TOKEN (Bearer token if server enforces auth)
"""
import os
import subprocess
from pathlib import Path
import uuid
import threading
import time
import asyncio
from typing import Any, Dict, Optional, Union, Callable

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError, register_custom_provider
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse

DEFAULT_BASE = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8000").rstrip("/")
_SCILLM_DEBUG = str(os.getenv("SCILLM_DEBUG", "")).lower() in {"1", "true", "yes"}


def _dbg(msg: str) -> None:
    if _SCILLM_DEBUG:
        try:
            print(f"[codeworld][debug] {msg}")
        except Exception:
            pass


class CodeWorldLLM(CustomLLM):
    def __init__(self) -> None:
        super().__init__()

    def _normalize_model_string(self, model: str) -> str:
        """
        Normalize provider alias strings so responses/manifests are canonical.
        - codeworld/mcts+auto -> codeworld/mcts:auto
        - other forms left as-is
        """
        try:
            if isinstance(model, str) and model.lower().startswith("codeworld/"):
                prefix, alias = model.split("/", 1)
                a = alias.strip().lower()
                if a == "mcts+auto":
                    a = "mcts:auto"
                return f"{prefix}/{a}"
        except Exception:
            pass
        return model

    def _resolve_base(self, api_base: Optional[str]) -> str:
        base = (api_base or DEFAULT_BASE).rstrip("/")
        _dbg(f"resolve_base base={base}")
        return base

    @staticmethod
    def _ping_healthz(base: str, timeout_s: float = 2.0) -> bool:
        try:
            with httpx.Client(timeout=timeout_s) as c:
                r = c.get(f"{base}/healthz")
                return 200 <= r.status_code < 300
        except Exception:
            return False

    def _autostart_bridge_if_needed(self, base_hint: str) -> str:
        # Decide whether auto-start is enabled: ON by default for localhost, else env-gated
        env_base = os.getenv("CODEWORLD_BASE")
        desired = (env_base or base_hint).rstrip("/")
        is_local = desired.startswith("http://127.0.0.1:") or desired.startswith("http://localhost:")
        auto_flag = os.getenv("SCILLM_AUTO_START_BRIDGES")
        auto_enabled = (is_local and auto_flag != "0") or (auto_flag == "1")
        if not auto_enabled:
            return desired
        if self._ping_healthz(desired, timeout_s=1.5):
            return desired
        # Start only the codeworld-bridge service via compose
        root = Path(__file__).resolve().parents[2]
        compose = root / "deploy" / "docker" / "compose.scillm.stack.yml"
        try:
            env = os.environ.copy()
            env.setdefault("COMPOSE_PROJECT_NAME", "scillm-bridges")
            # 1) If a container exists but is unhealthy, try a fast restart
            try:
                for name in ("scillm-bridges-codeworld-bridge-1", "codeworld-bridge", "codeworld-codeworld-1"):
                    subprocess.run(["docker","restart", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(5):
                    if self._ping_healthz(desired, timeout_s=1.0):
                        return desired
                    time.sleep(0.6)
            except Exception:
                pass
            subprocess.run([
                "docker", "compose", "-f", str(compose), "up", "-d", "codeworld-bridge"
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            # wait briefly
            for _ in range(10):
                if self._ping_healthz(desired, timeout_s=1.5):
                    break
        except Exception:
            pass
        return desired

    @staticmethod
    def _headers(api_key: Optional[str]) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        token = api_key or os.getenv("CODEWORLD_TOKEN")
        if token and not any(k.lower() == "authorization" for k in h):
            h["Authorization"] = f"Bearer {token}"
        return h

    def _build_payload(self, _model: str, _messages: list, optional_params: dict) -> Dict[str, Any]:
        """
        Build the canonical CodeWorld bridge payload with unified sugar handling
        to reduce cognitive load and mirror other SciLLM patterns.
        """
        p: Dict[str, Any] = {"messages": _messages}

        # Canonical envelope (if provided)
        items = optional_params.get("items")
        if items:
            p["items"] = items

        # Provider block (strategy, judge, etc.).
        provider = optional_params.get("provider") or {}
        if not isinstance(provider, dict):
            provider = {}
        args_block = provider.get("args") if isinstance(provider.get("args"), dict) else {}
        merged_args = dict(args_block)

        # Model alias support: "codeworld/mcts" injects strategy="mcts" unless caller set one
        try:
            alias: Optional[str] = None
            if isinstance(_model, str):
                lower = _model.lower().strip()
                if lower.startswith("codeworld/") and "/" in lower:
                    alias = lower.split("/", 1)[1]
                elif lower in ("mcts", "mcts:auto", "mcts+auto"):
                    alias = lower
            if alias is not None:
                if alias == "mcts" and "strategy" not in merged_args and "strategy" not in optional_params:
                    merged_args["strategy"] = "mcts"
                if alias in ("mcts:auto", "mcts+auto"):
                    if "strategy" not in merged_args and "strategy" not in optional_params:
                        merged_args["strategy"] = "mcts"
                    # enable autogeneration and pass a structured config understood by the bridge
                    # Prefer explicit caller params when present; fall back to envs
                    try:
                        n_env = int(os.getenv("CODEWORLD_MCTS_AUTO_N", "6"))
                    except Exception:
                        n_env = 6
                    try:
                        t_env = float(os.getenv("CODEWORLD_MCTS_AUTO_TEMPERATURE", "0.0"))
                    except Exception:
                        t_env = 0.0
                    try:
                        max_toks_env = int(os.getenv("CODEWORLD_MCTS_AUTO_MAX_TOKENS", "2000"))
                    except Exception:
                        max_toks_env = 2000
                    gen_model_env = os.getenv("CODEX_AGENT_MODEL", "gpt-5")
                    n_req = optional_params.get("n_variants")
                    t_req = optional_params.get("temperature")
                    max_toks_req = optional_params.get("max_tokens")
                    gen_model_req = optional_params.get("generator_model")
                    merged_args["autogenerate"] = {
                        "enabled": True,
                        "n": int(n_req if n_req is not None else n_env),
                        "temperature": float(t_req if t_req is not None else t_env),
                        "max_tokens": int(max_toks_req if max_toks_req is not None else max_toks_env),
                        "generator_model": str(gen_model_req if gen_model_req else gen_model_env),
                    }
                    gen_model = os.getenv("CODEWORLD_MCTS_AUTO_MODEL")
                    if gen_model:
                        merged_args.setdefault("generator_model", gen_model)
                    max_tokens_env = os.getenv("CODEWORLD_MCTS_AUTO_MAX_TOKENS")
                    if max_tokens_env:
                        try:
                            merged_args.setdefault("max_tokens", int(max_tokens_env))
                        except Exception:
                            pass
        except Exception:
            pass

        # Accept exploration_constant as a friendlier alias for uct_c
        if "exploration_constant" in optional_params and "uct_c" not in optional_params:
            try:
                optional_params["uct_c"] = optional_params["exploration_constant"]
            except Exception:
                pass

        # Unified sugar parameters (top-level) → fold into provider.args
        # Users can pass either strategy/strategy_config or individual knobs.
        for k in ("strategy", "strategy_config", "rollouts", "depth", "uct_c", "seed", "exploration_constant",
                  # autogeneration sugar
                  "autogenerate", "n_variants", "generator_model", "temperature", "max_tokens"):
            if k in optional_params and k not in merged_args:
                merged_args[k] = optional_params[k]

        # CI auto‑scaling for MCTS exploration when user didn't set explicit values
        try:
            ci_mode = (os.getenv("SCILLM_CI") == "1") or (os.getenv("GITHUB_ACTIONS", "").lower() == "true")
            if ci_mode and merged_args.get("strategy") == "mcts":
                if ("rollouts" not in args_block) and ("rollouts" not in optional_params):
                    merged_args.setdefault("rollouts", 24)
                if ("depth" not in args_block) and ("depth" not in optional_params):
                    merged_args.setdefault("depth", 5)
        except Exception:
            pass

        # One-time warnings (thread-safe) for alias conflicts and seed mismatches
        try:
            _warn_once = getattr(self.__class__, "_SCILLM_CODEWORLD_WARN_ONCE", None)
            if _warn_once is None:
                self.__class__._SCILLM_CODEWORLD_WARN_ONCE = {"exploration": False, "seed_mismatch": False}  # type: ignore[attr-defined]
                self.__class__._SCILLM_CODEWORLD_LOCK = threading.Lock()  # type: ignore[attr-defined]
            lock = getattr(self.__class__, "_SCILLM_CODEWORLD_LOCK")  # type: ignore[attr-defined]
            warn_once = getattr(self.__class__, "_SCILLM_CODEWORLD_WARN_ONCE")  # type: ignore[attr-defined]

            # exploration_constant vs uct_c conflict detection
            if "exploration_constant" in optional_params and "uct_c" in optional_params:
                try:
                    if float(optional_params["exploration_constant"]) != float(optional_params["uct_c"]):
                        if not os.getenv("SCILLM_SUPPRESS_EXPLORATION_ALIAS_WARNING"):
                            with lock:
                                if not warn_once["exploration"]:
                                    print("[codeworld][warn] exploration_constant and uct_c differ; using uct_c (canonical). Set SCILLM_SUPPRESS_EXPLORATION_ALIAS_WARNING=1 to suppress.")
                                    warn_once["exploration"] = True
                except Exception:
                    pass
            # If both made it into merged_args, drop exploration_constant to avoid ambiguity downstream
            if merged_args.get("strategy") == "mcts" and "uct_c" in merged_args and "exploration_constant" in merged_args:
                try:
                    merged_args.pop("exploration_constant", None)
                except Exception:
                    pass

            # Seed mismatch warning (per-request vs global)
            if merged_args.get("strategy") == "mcts":
                try:
                    req_seed = merged_args.get("seed")
                    global_seed = os.getenv("SCILLM_DETERMINISTIC_SEED")
                    if req_seed is not None and global_seed is not None and str(req_seed) != str(global_seed):
                        if not os.getenv("SCILLM_SUPPRESS_SEED_MISMATCH_WARNING"):
                            with lock:
                                if not warn_once["seed_mismatch"]:
                                    print(f"[codeworld][warn] per-request seed ({req_seed}) differs from SCILLM_DETERMINISTIC_SEED ({global_seed}). Per-request wins.")
                                    warn_once["seed_mismatch"] = True
                except Exception:
                    pass
        except Exception:
            pass

        if merged_args:
            provider["name"] = provider.get("name", "codeworld")
            provider["args"] = merged_args
        if provider:
            p["provider"] = provider

        if optional_params.get("options"):
            p["options"] = optional_params.get("options")

        # Back-compat CodeWorld aliases
        p.setdefault(
            "codeworld_metrics",
            optional_params.get("codeworld_metrics", ["correctness", "robustness", "speed", "brevity"]),
        )
        p.setdefault("codeworld_iterations", int(optional_params.get("codeworld_iterations", 3)))
        p.setdefault("codeworld_allowed_languages", optional_params.get("codeworld_allowed_languages", []))
        p.setdefault("request_timeout", float(optional_params.get("request_timeout", 60.0)))
        if optional_params.get("temperature") is not None:
            p["temperature"] = float(optional_params["temperature"])
        if optional_params.get("seed") is not None:
            p["seed"] = int(optional_params["seed"])
        # Avoid duplicate seed surfaces: prefer provider.args for MCTS (engine owns determinism)
        try:
            if isinstance(merged_args, dict) and merged_args.get("strategy") == "mcts" and "seed" in merged_args:
                # remove top-level seed to reduce ambiguity; provider.args['seed'] is the source of truth for strategy
                p.pop("seed", None)
        except Exception:
            pass
        if optional_params.get("return_artifacts") is not None:
            p["return_artifacts"] = bool(optional_params["return_artifacts"])
        # Debug summary (no secrets)
        try:
            args = (p.get("provider") or {}).get("args", {})
            _dbg(
                "payload model={} strategy={} rollouts={} depth={} uct_c={} autogen={} n_variants={}".format(
                    _model,
                    args.get("strategy"),
                    args.get("rollouts"),
                    args.get("depth"),
                    args.get("uct_c"),
                    args.get("autogenerate") or (args.get("n_variants") is not None),
                    args.get("n_variants"),
                )
            )
        except Exception:
            pass
        return p

    def _map_response(self, model_response: ModelResponse, data: Dict[str, Any], model: str) -> ModelResponse:
        # Build a concise message content while attaching full payload
        msg_text = ""
        try:
            s = data.get("summary") or {}
            if isinstance(s, dict):
                items = s.get("items")
                succ = s.get("succeeded")
                fail = s.get("failed")
                msg_text = f"CodeWorld: items={items}, succeeded={succ}, failed={fail}"
            else:
                msg_text = str(s)
        except Exception:
            msg_text = "CodeWorld: completed (see additional_kwargs.codeworld)"
        model_response.model = model
        try:
            model_response.choices[0].message.content = msg_text  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": msg_text}  # type: ignore[assignment]
        # Attach full payload
        try:
            model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
            model_response.additional_kwargs["codeworld"] = data
        except Exception:
            pass
        return model_response

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
        base = self._resolve_base(api_base)
        base = self._autostart_bridge_if_needed(base)
        normalized_model = self._normalize_model_string(model)
        trace_id = uuid.uuid4().hex
        _dbg(f"completion model={normalized_model} trace_id={trace_id}")
        payload = self._build_payload(model, messages, optional_params or {})
        hdr = {**headers, **self._headers(api_key)} if headers else self._headers(api_key)
        hdr.setdefault("X-Trace-Id", trace_id)
        budget = float(payload.get("request_timeout", 60.0))
        # Lightweight input summary
        try:
            items = payload.get("items") or []
            prov = (payload.get("provider") or {}).get("args", {})
            _dbg(
                f"pre http base={base} msgs={len(messages)} items={len(items)} rollouts={prov.get('rollouts')} depth={prov.get('depth')} uct_c={prov.get('uct_c')} autogen={prov.get('autogenerate') or prov.get('n_variants') is not None} trace_id={trace_id}"
            )
        except Exception:
            pass
        try:
            t0 = time.perf_counter()
            if isinstance(client, HTTPHandler):
                r = client.post(
                    f"{base}/bridge/complete", json=payload, headers=hdr, timeout=budget + 30.0
                )
                status = getattr(r, "status_code", 0)
                data = r.json() if hasattr(r, "json") else {}
            else:
                with httpx.Client(timeout=budget + 30.0, headers=hdr) as c:
                    resp = c.post(f"{base}/bridge/complete", json=payload)
                    status = resp.status_code
                    data = resp.json() if status in (200, 202) else {}
            dt = (time.perf_counter() - t0)
            _dbg(f"completion http_status={status} elapsed={dt:.2f}s budget={budget}s trace_id={trace_id}")
        except Exception as e:
            _dbg(f"completion exception={type(e).__name__}:{str(e)[:160]} trace_id={trace_id}")
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        if status == 200:
            return self._map_response(model_response, data, normalized_model)
        if status == 202:
            # Poll with exponential backoff up to 10s
            result_url = data.get("result_url") or ""
            t_end = time.time() + budget + 30.0
            backoff = 0.5
            if not result_url:
                raise CustomLLMError(status_code=500, message="bridge did not return result_url")
            while time.time() < t_end:
                try:
                    with httpx.Client(timeout=10.0, headers=hdr) as c:
                        rr = c.get(base + result_url) if result_url else None
                        if rr is None:
                            raise CustomLLMError(status_code=500, message="bridge result_url is invalid")
                        if rr.status_code == 200:
                            _dbg(f"poll OK -> 200 trace_id={trace_id}")
                            return self._map_response(model_response, rr.json(), normalized_model)
                        if rr.status_code != 202:
                            try:
                                payload = rr.json()
                                msg = payload.get("error") if isinstance(payload, dict) else payload
                            except Exception:
                                msg = rr.text
                            _dbg(f"poll error status={rr.status_code} msg={str(msg)[:120]} trace_id={trace_id}")
                            raise CustomLLMError(status_code=rr.status_code, message=str(msg)[:400])
                except Exception:
                    pass
                remaining = max(0.0, t_end - time.time())
                _dbg(f"poll 202 sleep={backoff:.1f}s remaining~{remaining:.1f}s trace_id={trace_id}")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)
            raise CustomLLMError(status_code=504, message="CodeWorld job did not complete within budget")
        # Error path
        try:
            msg = data.get("error") or str(data)[:200]
        except Exception:
            msg = f"CodeWorld bridge error (status {status})"
        raise CustomLLMError(status_code=status or 500, message=msg)

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
        base = self._resolve_base(api_base)
        base = self._autostart_bridge_if_needed(base)
        normalized_model = self._normalize_model_string(model)
        _dbg(f"acompletion model={normalized_model}")
        payload = self._build_payload(model, messages, optional_params or {})
        hdr = {**headers, **self._headers(api_key)} if headers else self._headers(api_key)
        budget = float(payload.get("request_timeout", 60.0))
        try:
            if isinstance(client, AsyncHTTPHandler):
                r = await client.post(
                    f"{base}/bridge/complete", json=payload, headers=hdr, timeout=budget + 30.0
                )
                status = getattr(r, "status_code", 0)
                data = r.json() if hasattr(r, "json") else {}
            else:
                async with httpx.AsyncClient(timeout=budget + 30.0, headers=hdr) as c:
                    resp = await c.post(f"{base}/bridge/complete", json=payload)
                    status = resp.status_code
                    data = resp.json() if status in (200, 202) else {}
            _dbg(f"acompletion http_status={status} budget={budget}s")
        except Exception as e:
            _dbg(f"acompletion exception={str(e)[:120]}")
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        if status == 200:
            return self._map_response(model_response, data, normalized_model)
        if status == 202:
            result_url = data.get("result_url") or ""
            t_end = time.time() + budget + 30.0
            if not result_url:
                raise CustomLLMError(status_code=500, message="bridge did not return result_url")
            async with httpx.AsyncClient(timeout=10.0, headers=hdr) as c:
                backoff = 0.5
                while time.time() < t_end:
                    try:
                        rr = await c.get(base + result_url)
                        if rr.status_code == 200:
                            _dbg("apoll OK -> 200")
                            return self._map_response(model_response, rr.json(), normalized_model)
                        if rr.status_code != 202:
                            try:
                                payload = rr.json()
                                msg = payload.get("error") if isinstance(payload, dict) else payload
                            except Exception:
                                msg = rr.text
                            _dbg(f"apoll error status={rr.status_code} msg={str(msg)[:120]}")
                            raise CustomLLMError(status_code=rr.status_code, message=str(msg)[:400])
                    except Exception:
                        pass
                    remaining = max(0.0, t_end - time.time())
                    _dbg(f"apoll 202 sleep={backoff:.1f}s remaining~{remaining:.1f}s")
                    await asyncio.sleep(backoff)  # type: ignore
                    backoff = min(backoff * 2.0, 10.0)
            raise CustomLLMError(status_code=504, message="CodeWorld job did not complete within budget")
        try:
            msg = data.get("error") or str(data)[:200]
        except Exception:
            msg = f"CodeWorld bridge error (status {status})"
        raise CustomLLMError(status_code=status or 500, message=msg)


# Register provider so Router can resolve custom_llm_provider="codeworld"
try:
    register_custom_provider("codeworld", CodeWorldLLM)
except Exception:
    pass
