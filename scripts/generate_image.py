#!/usr/bin/env python3
"""Generate an image from a repo prompt file and write PNG + receipt.

Default auth: Codex OAuth (`codex login`) via built-in image_gen in `codex exec`.
Optional auth: OpenAI API key via scillm POST /v1/images/generations (automation/CI).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PROGRESS_PREFIX = "scillm.image."


def emit_progress(event_type: str, **fields: Any) -> None:
    payload = {"type": f"{PROGRESS_PREFIX}{event_type}", **fields}
    print(json.dumps(payload), file=sys.stderr, flush=True)

DEFAULT_BASE = "http://127.0.0.1:4001"
DEFAULT_MODEL = "gpt-image-2"
PROMPT_MAX_CHARS = 32_000
DEFAULT_TIMEOUT_S = 600.0


def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_prompt(path: Path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"prompt file is empty: {path}")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise ValueError(
            f"prompt length {len(prompt)} exceeds limit {PROMPT_MAX_CHARS} characters"
        )
    return prompt


def _receipt_path_for_image(image_path: Path) -> Path:
    return image_path.with_name(f"{image_path.stem}_receipt.json")


def _default_timeout_s() -> float:
    raw = os.getenv("SCILLM_IMAGE_TIMEOUT_S")
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT_S
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(f"SCILLM_IMAGE_TIMEOUT_S must be numeric, got {raw!r}") from exc
    if timeout <= 0:
        raise ValueError(f"SCILLM_IMAGE_TIMEOUT_S must be positive, got {timeout!r}")
    return timeout


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
        handle.flush()


def _write_receipt(
    *,
    out: Path,
    receipt_file: Path,
    png_bytes: bytes,
    prompt_file: Path,
    prompt: str,
    auth: str,
    provider: str,
    model: str,
    caller_skill: str | None = None,
    endpoint: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    width, height = _png_dimensions(png_bytes)
    receipt: dict[str, Any] = {
        "ok": True,
        "auth": auth,
        "path": str(out),
        "width": width,
        "height": height,
        "sha256": _sha256_hex(png_bytes),
        "bytes": len(png_bytes),
        "model": model,
        "provider": provider,
        "prompt_file": str(prompt_file.resolve()),
        "prompt_chars": len(prompt),
        "prompt_sha256": _sha256_hex(prompt.encode("utf-8")),
        "endpoint": endpoint,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if caller_skill:
        receipt["caller_skill"] = caller_skill
    if extra:
        receipt.update(extra)
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    receipt["receipt_path"] = str(receipt_file)
    return receipt


def _codex_oauth_available() -> bool:
    try:
        from scillm.proxy.providers.auth import get_codex_credentials

        return get_codex_credentials() is not None
    except Exception:
        pass
    auth_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    tokens = data.get("tokens")
    if isinstance(tokens, dict) and tokens.get("access_token"):
        return True
    return bool(data.get("OPENAI_API_KEY"))


def _summarize_event(event: dict[str, Any]) -> str:
    kind = event.get("type", "?")
    if kind == "item.completed":
        item = event.get("item") or {}
        return f"{kind}:{item.get('type', '?')}"
    return str(kind)


def _newest_png_under(root: Path, since: float) -> Path | None:
    candidates = [
        p
        for p in root.rglob("*.png")
        if p.is_file() and p.stat().st_mtime >= since - 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _output_and_receipt_ready(out: Path, receipt_file: Path) -> bool:
    if not out.is_file() or out.stat().st_size == 0 or not receipt_file.is_file():
        return False
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if receipt.get("ok") is not True:
        return False
    recorded = str(receipt.get("sha256") or "")
    if not recorded:
        return True
    return recorded == _sha256_hex(out.read_bytes())


def generate_image_openai_api(
    *,
    prompt_file: Path,
    out: Path,
    model: str = DEFAULT_MODEL,
    quality: str = "high",
    size: str = "auto",
    background: str | None = None,
    output_format: str = "png",
    base_url: str = DEFAULT_BASE,
    master_key: str,
    caller_skill: str,
    receipt_path: Path | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    prompt = _load_prompt(prompt_file)
    emit_progress("started", auth="openai-api-key", model=model, prompt_chars=len(prompt))
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "response_format": "b64_json",
        "output_format": output_format,
        "n": 1,
    }
    if background:
        payload["background"] = background

    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
        "X-Caller-Skill": caller_skill,
    }
    url = f"{base_url.rstrip('/')}/v1/images/generations"

    started = time.monotonic()
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            raise RuntimeError(
                f"scillm images API HTTP {response.status_code}: {response.text[:500]}"
            )
        body = response.json()

    data = body.get("data") or []
    if not data:
        raise RuntimeError(f"scillm images API returned no data: {body}")
    item = data[0]
    b64 = item.get("b64_json")
    if not b64:
        raise RuntimeError(f"scillm images API missing b64_json: {item}")

    png_bytes = base64.b64decode(b64)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png_bytes)

    receipt_file = receipt_path or _receipt_path_for_image(out)
    receipt = _write_receipt(
        out=out,
        receipt_file=receipt_file,
        png_bytes=png_bytes,
        prompt_file=prompt_file,
        prompt=prompt,
        auth="openai-api-key",
        provider=str(body.get("provider") or "openai"),
        model=str(body.get("model") or model),
        caller_skill=caller_skill,
        endpoint="/v1/images/generations",
        extra={
            "quality": quality,
            "size": size,
            "model_requested": body.get("model_requested"),
            "revised_prompt": item.get("revised_prompt"),
            "scillm": body.get("scillm"),
        },
    )
    scillm_meta = body.get("scillm") or {}
    emit_progress(
        "completed",
        auth="openai-api-key",
        ok=True,
        terminal=True,
        elapsed_ms=scillm_meta.get("elapsed_ms") or int((time.monotonic() - started) * 1000),
        scillm_status=scillm_meta.get("status", "completed"),
        path=str(out),
        receipt_path=str(receipt_file),
        sha256=receipt["sha256"],
        width=receipt["width"],
        height=receipt["height"],
    )
    return receipt


def generate_image_codex_oauth(
    *,
    prompt_file: Path,
    out: Path,
    cwd: Path,
    receipt_path: Path | None = None,
    timeout_s: float | None = None,
    first_event_s: float = 30.0,
    bypass_sandbox: bool = True,
    bypass_hooks: bool = True,
    events_out: Path | None = None,
) -> dict[str, Any]:
    prompt = _load_prompt(prompt_file)
    timeout_s = _default_timeout_s() if timeout_s is None else timeout_s
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s!r}")
    emit_progress("started", auth="codex-oauth", prompt_chars=len(prompt))
    if not _codex_oauth_available():
        raise RuntimeError(
            "Codex OAuth not available. Run `codex login` (ChatGPT subscription) or use --auth openai-api-key."
        )
    out = out.resolve()
    receipt_file = (receipt_path or _receipt_path_for_image(out)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    exec_prompt = (
        "Use the built-in image_gen tool only (Codex OAuth subscription). "
        "Do NOT use scripts/image_gen.py, curl, or OPENAI_API_KEY.\n\n"
        f"1) Generate the image from this spec using image_gen.\n"
        f"2) Copy/move the final PNG to exactly: {out}\n"
        f"3) Write receipt JSON to exactly: {receipt_file} with keys "
        "ok, auth, path, width, height, sha256 (compute sha256 from the PNG bytes).\n"
        "4) Reply DONE only when both files exist.\n\n"
        "IMAGE SPEC:\n" + prompt
    )

    cmd = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "-C",
        str(cwd.resolve()),
    ]
    if bypass_sandbox:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if bypass_hooks:
        cmd.append("--dangerously-bypass-hook-trust")
    cmd.append(exec_prompt)

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None

    events: list[dict[str, Any]] = []
    event_count = 0
    thread_id: str | None = None

    while True:
        elapsed = time.monotonic() - started
        if event_count == 0 and elapsed > first_event_s:
            proc.kill()
            raise RuntimeError(
                f"no codex JSON events within {first_event_s:.0f}s (use stdin=closed; check codex login)"
            )
        if elapsed > timeout_s:
            proc.kill()
            raise RuntimeError(f"codex exec exceeded timeout {timeout_s:.0f}s after {event_count} events")

        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if not ready:
            if _output_and_receipt_ready(out, receipt_file):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
            continue
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_count += 1
        events.append(event)
        if events_out:
            _append_jsonl(events_out, event)
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        print(f"[codex +{elapsed:5.1f}s] #{event_count} {_summarize_event(event)}", flush=True)

    rc = proc.wait()
    stderr = proc.stderr.read().strip() if proc.stderr else ""

    if rc != 0 and not _output_and_receipt_ready(out, receipt_file):
        raise RuntimeError(f"codex exec failed rc={rc}: {stderr[:500]}")

    if not out.is_file() or out.stat().st_size == 0:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        since = started + time.time() - time.monotonic() - 5
        search_roots = [codex_home / "generated_images"]
        if thread_id:
            search_roots.insert(0, codex_home / "generated_images" / thread_id)
        fallback: Path | None = None
        for root in search_roots:
            if root.is_dir():
                fallback = _newest_png_under(root, since)
                if fallback:
                    break
        if fallback is None:
            raise RuntimeError(
                "codex finished but workspace PNG missing and no recent ~/.codex/generated_images PNG found"
            )
        shutil.copy2(fallback, out)
        print(f"copied fallback {fallback} -> {out}", flush=True)

    png_bytes = out.read_bytes()
    if receipt_file.is_file():
        try:
            saved = json.loads(receipt_file.read_text(encoding="utf-8"))
            if saved.get("ok"):
                saved.setdefault("auth", "codex-oauth")
                saved.setdefault("path", str(out))
                saved["receipt_path"] = str(receipt_file)
                emit_progress(
                    "completed",
                    auth="codex-oauth",
                    ok=True,
                    terminal=True,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    scillm_status="completed",
                    path=str(out),
                    receipt_path=str(receipt_file),
                    sha256=saved.get("sha256"),
                    width=saved.get("width"),
                    height=saved.get("height"),
                    codex_events=event_count,
                )
                return saved
        except json.JSONDecodeError:
            pass

    receipt = _write_receipt(
        out=out,
        receipt_file=receipt_file,
        png_bytes=png_bytes,
        prompt_file=prompt_file,
        prompt=prompt,
        auth="codex-oauth",
        provider="codex-oauth",
        model="image_gen",
        caller_skill=None,
        endpoint="codex exec (image_gen)",
        extra={"codex_thread_id": thread_id, "codex_events": event_count},
    )
    emit_progress(
        "completed",
        auth="codex-oauth",
        ok=True,
        terminal=True,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        scillm_status="completed",
        path=str(out),
        receipt_path=str(receipt_file),
        sha256=receipt["sha256"],
        width=receipt["width"],
        height=receipt["height"],
        codex_events=event_count,
    )
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Default: Codex OAuth (codex login)\n"
            "  python scripts/generate_image.py \\\n"
            "    --prompt-file examples/image-prompts/sample-icon.prompt.md \\\n"
            "    --out artifacts/images/sample-icon.png\n\n"
            "  # CI / explicit API key path\n"
            "  python scripts/generate_image.py --auth openai-api-key \\\n"
            "    --prompt-file … --out … --caller-skill my-project\n"
        ),
    )
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--auth",
        choices=["codex-oauth", "openai-api-key"],
        default=os.getenv("SCILLM_IMAGE_AUTH", "codex-oauth"),
    )
    parser.add_argument("-C", "--cwd", type=Path, default=Path.cwd(), help="Workspace for codex exec")
    parser.add_argument("--events-out", type=Path, help="Save codex JSONL when using codex-oauth")
    parser.add_argument("--model", default=os.getenv("SCILLM_IMAGE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--quality", default=os.getenv("SCILLM_IMAGE_QUALITY", "high"))
    parser.add_argument("--size", default=os.getenv("SCILLM_IMAGE_SIZE", "auto"))
    parser.add_argument("--background", choices=["transparent", "opaque", "auto"])
    parser.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    parser.add_argument("--base-url", default=os.getenv("SCILLM_BASE", os.getenv("PROXY_BASE", DEFAULT_BASE)))
    parser.add_argument(
        "--master-key",
        default=os.getenv("MASTER_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123")),
    )
    parser.add_argument("--caller-skill", default=os.getenv("X_CALLER_SKILL", "scillm-generate-image"))
    parser.add_argument("--timeout-s", type=float, default=None)
    parser.add_argument("--json", action="store_true", dest="json_out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.auth == "codex-oauth":
            receipt = generate_image_codex_oauth(
                prompt_file=args.prompt_file,
                out=args.out,
                cwd=args.cwd,
                receipt_path=args.receipt,
                timeout_s=args.timeout_s,
                events_out=args.events_out,
            )
        else:
            receipt = generate_image_openai_api(
                prompt_file=args.prompt_file,
                out=args.out,
                model=args.model,
                quality=args.quality,
                size=args.size,
                background=args.background,
                output_format=args.output_format,
                base_url=args.base_url,
                master_key=args.master_key,
                caller_skill=args.caller_skill,
                receipt_path=args.receipt,
                timeout_s=args.timeout_s,
            )
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        emit_progress("failed", ok=False, terminal=True, error=str(exc))
        print(f"generate_image: {exc}", file=sys.stderr)
        return 1

    if args.json_out:
        print(json.dumps(receipt, indent=2))
    else:
        print(
            f"wrote {receipt['path']} ({receipt.get('width')}x{receipt.get('height')}, "
            f"{receipt.get('bytes')} bytes) auth={receipt.get('auth')}"
        )
        print(f"receipt {receipt['receipt_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
