from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
import asyncio
from urllib.parse import urlparse

# Optional robust retry package
try:
    from tenacity import retry, stop_after_delay, wait_exponential_jitter
except Exception:  # pragma: no cover - optional dependency
    retry = None  # type: ignore
    stop_after_delay = None  # type: ignore
    wait_exponential_jitter = None  # type: ignore


@dataclass
class Chute:
    name: str
    base_url: str
    api_key: str
    warmup_seconds: float | None = None


def _run(cmd: List[str], timeout: float = 300.0, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True, cwd=cwd)


def _env(val: Optional[str], key: str) -> Optional[str]:
    return val if val else os.getenv(key)


def _headers_for(key: str, style: str = "x-api-key") -> Dict[str, str]:
    if style == "x-api-key":
        return {"x-api-key": key, "content-type": "application/json"}
    if style == "authorization-raw":
        return {"authorization": key, "content-type": "application/json"}
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


_CLIENTS: Dict[str, httpx.Client] = {}


def _origin(u: str) -> str:
    p = urlparse(u)
    host = f"{p.scheme}://{p.netloc}"
    return host


def _get_client(origin: str, timeout: float) -> httpx.Client:
    c = _CLIENTS.get(origin)
    if c is None:
        c = httpx.Client(timeout=timeout)
        _CLIENTS[origin] = c
    else:
        # update timeout if larger
        try:
            if timeout > c.timeout.read:  # type: ignore[attr-defined]
                _CLIENTS[origin] = httpx.Client(timeout=timeout)
                c = _CLIENTS[origin]
        except Exception:
            pass
    return c


def _http_json(method: str, url: str, headers: Dict[str, str], json_body: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> tuple[int, Any]:
    c = _get_client(_origin(url), timeout)
    try:
        r = c.request(method.upper(), url, headers=headers, json=json_body)
        try:
            b = r.json()
        except Exception:
            b = r.text
        return r.status_code, b
    except Exception as e:
        return 599, {"error": str(e)}


def _http_json_h(method: str, url: str, headers: Dict[str, str], json_body: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Tuple[int, Any, Dict[str, str]]:
    c = _get_client(_origin(url), timeout)
    try:
        r = c.request(method.upper(), url, headers=headers, json=json_body)
        try:
            b = r.json()
        except Exception:
            b = r.text
        return r.status_code, b, dict(r.headers)
    except Exception as e:
        return 599, {"error": str(e)}, {}


def _discover_auth_style(base_url: str, api_key: str) -> str:
    # Probe /v1/models with header variants
    for style in ("x-api-key", "authorization-raw", "authorization-bearer"):
        hdr = _headers_for(api_key, style)
        st, _ = _http_json("GET", f"{base_url.rstrip('/')}/models", headers=hdr, timeout=10.0)
        if st == 200:
            return style
    return "x-api-key"


def _warmup_kick(base_url: str, api_key: str) -> None:
    """Best-effort warmup kick via cord, per Chutes docs.

    Tries known auth styles; ignores all errors. Startup should remain fast
    and non-blocking; readiness continues to gate on /v1/models == 200.
    """
    url = f"{base_url.rstrip('/')}/warmup/kick"
    for style in ("x-api-key", "authorization-raw", "authorization-bearer"):
        try:
            _http_json("POST", url, headers=_headers_for(api_key, style), json_body={"ok": True}, timeout=3.0)
        except Exception:
            pass


def _warmup_status(base_url: str, api_key: str, style: str) -> Optional[Dict[str, Any]]:
    """Fetch warmup status if the chute exposes the cord.

    Returns a dict with keys like state|phase|progress when available; otherwise None.
    """
    st, body = _http_json("GET", f"{base_url.rstrip('/')}/warmup/status", headers=_headers_for(api_key, style), timeout=3.0)
    if st == 200 and isinstance(body, dict):
        return body
    return None


def _wait_ready(base_url: str, api_key: str, timeout_s: float = 180.0) -> str:
    """Wait until chute reports ready via /v1/models == 200.

    Uses tenacity exponential backoff with jitter when available; falls back
    to a simple polling loop otherwise. Returns the detected auth style name.
    """
    def _probe() -> str:
        style = _discover_auth_style(base_url, api_key)
        # Best-effort: surface warmup progress if cords are present (non-blocking)
        try:
            _warmup_status(base_url, api_key, style)
        except Exception:
            pass
        st, _ = _http_json(
            "GET",
            f"{base_url.rstrip('/')}/models",
            headers=_headers_for(api_key, style),
            timeout=10.0,
        )
        if st == 200:
            # Optional tiny prewarm request for JSON-path sanity
            if (os.getenv("CHUTES_PREWARM", "").lower() in {"1","true","yes"}):
                try:
                    models = _list_models(base_url, api_key, style)
                    m = models[0] if models else None
                    if m:
                        payload = {
                            "model": m,
                            "messages": [{"role":"user","content": "Return only {\"ok\":true} as JSON."}],
                            "response_format": {"type":"json_object"},
                            "max_tokens": 8,
                            "temperature": 0,
                        }
                        _http_json(
                            "POST",
                            f"{base_url.rstrip('/')}/chat/completions",
                            headers=_headers_for(api_key, style),
                            json_body=payload,
                            timeout=8.0,
                        )
                except Exception:
                    pass
            return style
        raise RuntimeError(f"not-ready status={st}")

    if retry and stop_after_delay and wait_exponential_jitter:
        @_retry_decorator := retry(
            wait=wait_exponential_jitter(initial=0.5, max=5.0),
            stop=stop_after_delay(timeout_s),
            reraise=True,
        )
        def _wrapped() -> str:  # type: ignore
            return _probe()
        return _retry_decorator(_wrapped)()

    # Fallback: simple loop
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            return _probe()
        except Exception:
            time.sleep(1.0)
    raise RuntimeError(f"Chute not ready: {base_url}")


def _normalize_id(s: str) -> str:
    s = s.strip()
    if s.startswith("openai/"):
        s = s.split("/", 1)[1]
    return s.lower().replace("_", "-")


def _list_models(base_url: str, api_key: str, style: str) -> List[str]:
    st, body = _http_json("GET", f"{base_url.rstrip('/')}/models", headers=_headers_for(api_key, style), timeout=15.0)
    out: List[str] = []
    if st == 200 and isinstance(body, dict):
        for d in body.get("data", []) or []:
            mid = str(d.get("id") or "").strip()
            if mid:
                out.append(mid)
    return out


def _resolve_model_alias(base_url: str, api_key: str, style: str, requested: str) -> Optional[str]:
    strict = (os.getenv("CHUTES_MODEL_STRICT", "").lower() in {"1","true","yes"})
    mapping = os.getenv("CHUTES_MODEL_MAP", "")
    mp: Dict[str,str] = {}
    if mapping:
        for part in mapping.split(","):
            if ":" in part:
                k,v = part.split(":",1)
                mp[_normalize_id(k)] = v.strip()
    req_n = _normalize_id(requested)
    if mp.get(req_n):
        return mp[req_n]
    if strict:
        return None
    models = _list_models(base_url, api_key, style)
    if not models:
        return None
    # Exact normalized match
    for m in models:
        if _normalize_id(m) == req_n:
            return m
    # Family prefix: provider/name without size
    fam = req_n.split(":",1)[-1]
    fam = fam.split("-")[0]
    best = None
    for m in models:
        mn = _normalize_id(m)
        if mn.startswith(fam):
            best = m
            break
    return best


def _list_chutes() -> List[str]:
    p = _run(["chutes", "chutes", "list"], timeout=120.0)
    if p.returncode != 0:
        raise RuntimeError(f"chutes list failed: {p.stderr.strip()}")
    # Best-effort parse: look for lines that look like fully qualified names or ids
    names: List[str] = []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # crude filter: tokens with at least one dash or underscore
        if any(c in line for c in ("-", "_")) and ":" not in line and "Found 0" not in line and "Loading chutes config" not in line:
            names.append(line.split()[0])
    return names


def _find_url_for_name(name: str) -> Optional[str]:
    # Try direct get first (prints JSON-ish details now; may or may not include a URL)
    p = _run(["chutes", "chutes", "get", name], timeout=120.0)
    if p.returncode == 0:
        # First: look for explicit URL in stdout lines
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("https://") and ".chutes.ai" in line:
                base = line.split()[0].rstrip("/")
                return base + ("/v1" if not base.endswith("/v1") else "")
        # Next: parse slug from JSON and synthesize URL
        # Try a simple regex for slug
        try:
            import re as _re
            m = _re.search(r'"slug"\s*:\s*"([a-z0-9\-]+)"', p.stdout or "", flags=_re.IGNORECASE)
            if m:
                slug = m.group(1)
                return f"https://{slug}.chutes.ai/v1"
        except Exception:
            pass
    # Fallback: list and attempt a match on name tokens
    for n in _list_chutes():
        if n.endswith(name) or n == name:
            p2 = _run(["chutes", "chutes", "get", n], timeout=120.0)
            if p2.returncode == 0:
                for line in (p2.stdout or "").splitlines():
                    line = line.strip()
                    if line.startswith("https://") and ".chutes.ai" in line:
                        base = line.split()[0].rstrip("/")
                        return base + ("/v1" if not base.endswith("/v1") else "")
    return None


_CACHE: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.monotonic()


def _sdk_deploy_via_hook(hook: str, name: str) -> Optional[str]:
    """Call a user-provided hook 'module:function' that performs a deploy and returns base URL.
    The function signature should be f(name:str) -> str|None.
    """
    try:
        mod_name, func_name = hook.split(":", 1) if ":" in hook else hook.rsplit(".", 1)
        mod = import_module(mod_name)
        func = getattr(mod, func_name)
        base = func(name)
        return base.rstrip("/") + "/v1" if base else None
    except Exception:
        return None


def ensure(
    name: str,
    *,
    api_key: Optional[str] = None,
    template: Optional[str] = None,
    accept_fee: bool = True,
    wait_seconds: float = 180.0,
    ttl_sec: Optional[float] = None,
    prefer_sdk: bool = False,
) -> Chute:
    """Ensure a chute named `name` exists and is ready. Returns Chute with base_url including /v1.

    - Uses `chutes build <name>:chute --wait` then `chutes deploy <name>:chute --accept-fee`.
    - If already exists, skips build.
    - Determines auth header style automatically.
    """
    api_key = _env(api_key, "CHUTES_API_KEY")
    if not api_key:
        raise ValueError("CHUTES_API_KEY not set; pass api_key or export env")

    tpl = template or os.getenv("CHUTES_TEMPLATE", f"{name}:chute")

    # TTL reuse
    ttl_env = os.getenv("CHUTES_TTL_SEC")
    ttl = ttl_sec if ttl_sec is not None else (float(ttl_env) if ttl_env else 0.0)
    ent = _CACHE.get(name)
    if ent and ttl > 0 and ent.get("expires_at", 0.0) > _now():
        return Chute(name=name, base_url=ent["base_url"], api_key=api_key)

    # Try find existing
    base = _find_url_for_name(name)
    if not base:
        # Try SDK hook if preferred or configured
        sdk_hook = os.getenv("CHUTES_SDK_DEPLOY_HOOK")
        if prefer_sdk or sdk_hook:
            base = _sdk_deploy_via_hook(sdk_hook or "", name)
        if not base:
            # Build
            tpl_dir = os.getenv("CHUTES_TEMPLATE_DIR")
            _run(["chutes", "build", tpl, "--wait"], cwd=tpl_dir)  # ignore return to allow 'no need to build'
            # Deploy via CLI (non-interactive)
            cmd = ["bash", "-lc", f"yes | chutes deploy {shlex.quote(tpl)} {'--accept-fee' if accept_fee else ''}"]
            p = _run(cmd, timeout=300.0, cwd=tpl_dir)
            if p.returncode != 0:
                raise RuntimeError(f"chutes deploy failed: {p.stderr.strip()}")
            # Optional post-deploy hook (autoscale or scale commands), explicit and opt-in
            post = os.getenv("CHUTES_POST_DEPLOY_CMD")
            if post:
                try:
                    _run(["bash","-lc", post], timeout=60.0, cwd=tpl_dir)
                except Exception:
                    pass
        # Re-list to get URL
        base = _find_url_for_name(name)
        if not base:
            raise RuntimeError("chutes deploy succeeded but URL not found in list")

    # Kick background warmup (non-blocking) and wait until ready; record warmup time
    t0 = time.monotonic()
    try:
        _warmup_kick(base, api_key)
    except Exception:
        pass
    style = _wait_ready(base, api_key, timeout_s=wait_seconds)
    elapsed = max(0.0, time.monotonic() - t0)
    # Store preferred style in env cache for later calls
    os.environ.setdefault("CHUTES_AUTH_STYLE", style)
    if ttl > 0:
        _CACHE[name] = {"base_url": base, "style": style, "expires_at": _now() + ttl}
    return Chute(name=name, base_url=base, api_key=api_key, warmup_seconds=elapsed)


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    v = os.getenv(name)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def infer(
    chute: Chute,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    response_format: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    base = chute.base_url.rstrip("/")
    style = os.getenv("CHUTES_AUTH_STYLE", "x-api-key")
    payload: Dict[str, Any] = {"model": model, "messages": messages}
    if response_format:
        payload["response_format"] = response_format
    # Cost-aware defaults from env
    if max_tokens is None:
        mt = os.getenv("CHUTES_MAX_TOKENS")
        if mt:
            try:
                max_tokens = int(mt)
            except Exception:
                pass
    if temperature is None:
        temperature = _env_float("CHUTES_TEMPERATURE")
    if top_p is None:
        top_p = _env_float("CHUTES_TOP_P")
    if seed is None:
        s = os.getenv("CHUTES_SEED")
        if s:
            try:
                seed = int(s)
            except Exception:
                pass
    # Apply controls if provided
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if seed is not None:
        payload["seed"] = seed
    # Try header sequence, starting with cached style
    backoff_base = float(os.getenv("CHUTES_BACKOFF_BASE", "0.6"))
    backoff_cap = float(os.getenv("CHUTES_BACKOFF_CAP", "4.0"))
    max_attempts = int(os.getenv("CHUTES_BACKOFF_MAX_ATTEMPTS", "3"))
    for style in (style, "x-api-key", "authorization-raw", "authorization-bearer"):
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            st, body, hdrs = _http_json_h("POST", f"{base}/chat/completions", headers=_headers_for(chute.api_key, style), json_body=payload, timeout=timeout)
            if st == 200 and isinstance(body, dict):
                return body
            # If model not found because of vendor prefix, retry without it
            if st == 404 and isinstance(body, dict) and str(body.get("detail", "")).startswith("model not found") and model.startswith("openai/"):
                payload["model"] = model.split("/", 1)[-1]
                st2, body2, _ = _http_json_h("POST", f"{base}/chat/completions", headers=_headers_for(chute.api_key, style), json_body=payload, timeout=timeout)
                if st2 == 200 and isinstance(body2, dict):
                    return body2
                # fall-through to aliasing if still failing
            # Alias resolution on 404 (closest model)
            if st == 404 and isinstance(body, dict) and str(body.get("detail", "")).startswith("model not found"):
                alt = _resolve_model_alias(base, chute.api_key, style, payload["model"])  # type: ignore
                if alt and alt != payload["model"]:
                    payload["model"] = alt
                    st2, body2, _ = _http_json_h("POST", f"{base}/chat/completions", headers=_headers_for(chute.api_key, style), json_body=payload, timeout=timeout)
                    if st2 == 200 and isinstance(body2, dict):
                        return body2
            # Retry on rate-limit/server errors with Retry-After
            if st in (429, 500, 502, 503):
                ra = hdrs.get("retry-after") or hdrs.get("Retry-After")
                if ra:
                    try:
                        delay = float(ra)
                    except Exception:
                        delay = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                else:
                    delay = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                try:
                    time.sleep(delay)
                except Exception:
                    pass
                continue
            # For other statuses, break to try next style
            break
    raise RuntimeError(f"inference failed on {base}")


def close(name: str) -> None:
    # Optional pre-delete autoscale hook (explicit command string)
    hook = os.getenv("CHUTES_PRE_DELETE_CMD")
    if hook:
        try:
            _run(["bash", "-lc", hook], timeout=60.0)
        except Exception:
            pass
    p = _run(["chutes", "chutes", "delete", name], timeout=120.0)
    if p.returncode != 0:
        raise RuntimeError(f"chutes delete failed: {p.stderr.strip()}")


class ChuteSession:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs
        self.chute: Optional[Chute] = None

    def __enter__(self) -> Chute:
        self.chute = ensure(self.name, **self.kwargs)
        return self.chute

    def __exit__(self, exc_type, exc, tb) -> None:
        # Optional: do not auto-delete by default; caller can call close()
        pass


# Async wrappers (non-blocking via thread offload)
async def aensure(*args, **kwargs) -> Chute:
    return await asyncio.to_thread(ensure, *args, **kwargs)


async def ainfer(*args, **kwargs) -> Dict[str, Any]:
    return await asyncio.to_thread(infer, *args, **kwargs)


async def aclose(*args, **kwargs) -> None:
    await asyncio.to_thread(close, *args, **kwargs)
