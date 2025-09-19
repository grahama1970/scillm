"""
Lightweight image helpers to reduce boilerplate when building vision prompts.

Optional deps:
- Pillow (PIL) for local image load/resize/re-encode
- httpx for remote fetches

APIs (stable, small):
- compress_image(path, *, max_kb=1024, cache_dir=None) -> data_url
- fetch_remote_image(url, *, cache_dir=None, timeout=10.0, max_kb=1024) -> data_url

Both return data URLs like: data:image/jpeg;base64,<...>

Notes:
- cache_dir stores re-encoded bytes keyed by SHA-256(url or path+params)
- This module is optional. Import errors will explain how to enable extras.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
from typing import Optional


def _ensure_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)


def _to_cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "ignore"))
    return h.hexdigest()


def _b64_data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def compress_image(path: str | Path, *, max_kb: int = 1024, cache_dir: Optional[str | Path] = None) -> str:
    try:
        from PIL import Image  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ImportError("Install Pillow: pip install pillow (or `.[images]`)") from e

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))

    cache_path: Optional[Path] = None
    if cache_dir:
        _ensure_dir(Path(cache_dir))
        cache_path = Path(cache_dir) / ( _to_cache_key(str(p.resolve()), str(max_kb)) + ".bin" )
        if cache_path.is_file():
            b = cache_path.read_bytes()
            mime = "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png"
            return _b64_data_url(mime, b)

    with Image.open(str(p)) as im:
        im = im.convert("RGB")  # normalize
        # Simple quality loop to target max_kb
        low, high = 40, 95
        best = None
        while low <= high:
            q = (low + high) // 2
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_kb * 1024:
                best = data
                low = q + 1
            else:
                high = q - 1
        out = best if best is not None else data  # type: ignore[name-defined]
        if cache_path:
            cache_path.write_bytes(out)
        return _b64_data_url("image/jpeg", out)


async def fetch_remote_image(
    url: str,
    *,
    cache_dir: Optional[str | Path] = None,
    timeout: float = 10.0,
    max_kb: int = 1024,
) -> str:
    try:
        import httpx  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ImportError("Install httpx: pip install httpx (or `.[images]`)") from e
    try:
        from PIL import Image  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ImportError("Install Pillow: pip install pillow (or `.[images]`)") from e

    cache_path: Optional[Path] = None
    if cache_dir:
        _ensure_dir(Path(cache_dir))
        cache_path = Path(cache_dir) / ( _to_cache_key(url, str(max_kb)) + ".bin" )
        if cache_path.is_file():
            b = cache_path.read_bytes()
            mime = "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png"
            return _b64_data_url(mime, b)

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        raw = r.content

    # re-encode & cap size
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        data = buf.getvalue()
        if len(data) > max_kb * 1024:
            # fall back to stronger compression
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=60, optimize=True)
            data = buf.getvalue()
    if cache_path:
        cache_path.write_bytes(data)
    return _b64_data_url("image/jpeg", data)

