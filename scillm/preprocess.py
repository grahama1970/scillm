from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple


def _is_http_url(v: str) -> bool:
    v = (v or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def _html_to_text(html: str) -> str:
    try:
        # Avoid a hard dependency; fall back to a crude strip
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]
        return "\n".join(lines)
    except Exception:
        # crude fallback
        import re

        txt = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
        txt = re.sub(r"<style[\s\S]*?</style>", " ", txt, flags=re.I)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = " ".join(txt.split())
        return txt


def _read_file(path: str, *, max_bytes: int) -> Tuple[str, str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            text = raw.decode("latin-1", "replace")
        content_type = "text/plain"
        if path.lower().endswith(('.html', '.htm')):
            text = _html_to_text(text)
            content_type = "text/html"
        return text, content_type
    except Exception as e:
        return f"READ_ERROR: {e}", "text/plain"


def _http_get(url: str, *, max_bytes: int, timeout: float) -> Tuple[str, str]:
    try:
        import httpx  # type: ignore

        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        body = r.content[: max_bytes]
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = body.decode("latin-1", "replace")
        ctype = r.headers.get("content-type", "text/plain").split(";")[0].strip().lower()
        if ctype.startswith("text/html"):
            text = _html_to_text(text)
        return text, ctype
    except Exception as e:
        return f"FETCH_ERROR: {e}", "text/plain"


def _http_get_binary(url: str, *, timeout: float) -> Tuple[bytes, str]:
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
        return data, ctype.split(";")[0].strip().lower()
    except Exception as e:
        raise RuntimeError(f"FETCH_ERROR: {e}") from e


def expand_requests_io(
    requests: List[Dict[str, Any]],
    *,
    max_bytes: int = 1_000_000,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """Detect 'url' or 'file_path' keys and expand them into message content.

    - If messages already exist, appends a user message with fetched text.
    - If no messages, creates a single user message from the fetched text.
    - Trims content to max_bytes and converts HTML → text.
    - Does NOT auto-fetch plain strings; only explicit keys are processed.
    """
    out: List[Dict[str, Any]] = []
    # Default ON for simple agent experience; can be disabled via SCILLM_AUTO_IMAGE_DATAURL=0
    auto_image = str(os.getenv("SCILLM_AUTO_IMAGE_DATAURL", "1")).lower() in {"1","true","yes","on"}
    inline_remote_images = str(os.getenv("SCILLM_INLINE_REMOTE_IMAGES", "0")).lower() in {"1","true","yes","on"}
    image_exts = {".jpg",".jpeg",".png",".gif",".webp",".bmp"}
    inline_cache: Dict[str, str] = {}
    for req in requests or []:
        r = dict(req or {})
        text_parts: List[str] = []
        # single url/file_path
        url = r.get("url")
        fpath = r.get("file_path")
        if isinstance(url, str) and _is_http_url(url):
            # Heuristic: if looks like an image and auto_image is on, append as image_url part
            if auto_image and any(url.lower().endswith(ext) for ext in image_exts):
                msgs = list(r.get("messages") or [])
                image_url_payload = url
                if inline_remote_images:
                    try:
                        body, content_type = _http_get_binary(url, timeout=timeout)
                        import base64

                        encoded = base64.b64encode(body).decode("ascii")
                        image_url_payload = f"data:{content_type};base64,{encoded}"
                    except Exception:
                        pass
                msgs.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Image input."},
                        {"type": "image_url", "image_url": {"url": image_url_payload}},
                    ],
                })
                r["messages"] = msgs
            else:
                text, _ = _http_get(url, max_bytes=max_bytes, timeout=timeout)
                text_parts.append(f"URL: {url}\n\n{text}\n")
        if isinstance(fpath, str) and os.path.exists(fpath):
            if auto_image and any(fpath.lower().endswith(ext) for ext in image_exts):
                try:
                    import base64, mimetypes
                    mime, _ = mimetypes.guess_type(fpath)
                    mime = mime or "application/octet-stream"
                    with open(fpath, "rb") as fh:
                        b64 = base64.b64encode(fh.read()).decode("ascii")
                    data_url = f"data:{mime};base64,{b64}"
                    msgs = list(r.get("messages") or [])
                    msgs.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": os.path.basename(fpath)},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    })
                    r["messages"] = msgs
                except Exception:
                    txt, _ = _read_file(fpath, max_bytes=max_bytes)
                    text_parts.append(f"FILE: {os.path.basename(fpath)}\n\n{txt}\n")
            else:
                text, _ = _read_file(fpath, max_bytes=max_bytes)
                text_parts.append(f"FILE: {os.path.basename(fpath)}\n\n{text}\n")
        # multi-urls / multi-paths
        artifact_urls = list((r.get("artifacts") or {}).get("urls") or [])
        for u in (r.get("urls") or []):
            if isinstance(u, str) and _is_http_url(u):
                txt, _ = _http_get(u, max_bytes=max_bytes, timeout=timeout)
                text_parts.append(f"URL: {u}\n\n{txt}\n")
        for u in artifact_urls:
            if isinstance(u, str) and _is_http_url(u) and inline_remote_images:
                if u not in inline_cache:
                    try:
                        body, ctype = _http_get_binary(u, timeout=timeout)
                        import base64

                        inline_cache[u] = f"data:{ctype};base64,{base64.b64encode(body).decode('ascii')}"
                    except Exception:
                        inline_cache[u] = u
                # store replacement for later message rewrite if needed
                r.setdefault("_artifacts_inline", {})[u] = inline_cache[u]
        for p in (r.get("paths") or []):
            if isinstance(p, str) and os.path.exists(p):
                txt, _ = _read_file(p, max_bytes=max_bytes)
                text_parts.append(f"FILE: {os.path.basename(p)}\n\n{txt}\n")

        if inline_remote_images:
            msgs = list(r.get("messages") or [])
            if msgs:
                _inline_message_images(msgs, inline_cache, timeout=timeout)
                r["messages"] = msgs
        if text_parts:
            body = "\n\n".join(text_parts)
            msgs = list(r.get("messages") or [])
            msgs.append({"role": "user", "content": body})
            r["messages"] = msgs
        out.append(r)
    return out


def _inline_message_images(messages: List[Dict[str, Any]], cache: Dict[str, str], *, timeout: float) -> None:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    payload = part.get("image_url") or {}
                    if isinstance(payload, dict):
                        url_val = payload.get("url")
                        if isinstance(url_val, str) and _is_http_url(url_val):
                            if url_val not in cache:
                                try:
                                    body, ctype = _http_get_binary(url_val, timeout=timeout)
                                    import base64

                                    encoded = base64.b64encode(body).decode("ascii")
                                    cache[url_val] = f"data:{ctype};base64,{encoded}"
                                except Exception:
                                    cache[url_val] = url_val
                            payload["url"] = cache[url_val]
