"""Small command-line client for the local scillm proxy."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx


from scillm.dag_phart import run_phart_on_dag, run_phart_on_file


def _load_dag_arg(args: argparse.Namespace) -> dict[str, Any]:
    if args.dag_file:
        return json.loads(Path(args.dag_file).expanduser().read_text(encoding="utf-8"))
    if args.dag_json:
        return json.loads(args.dag_json)
    if not sys.stdin.isatty():
        return json.loads(sys.stdin.read())
    raise ValueError("dag commands require --dag-file, --dag-json, or stdin JSON")

def _load_seed_dag_arg(args: argparse.Namespace) -> dict[str, Any] | None:
    if getattr(args, "seed_dag_file", None):
        return json.loads(Path(args.seed_dag_file).expanduser().read_text(encoding="utf-8"))
    if getattr(args, "seed_dag_json", None):
        return json.loads(args.seed_dag_json)
    return None


def _emit_phart_result(result) -> int:
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            sys.stderr.write("\n")
    return 0 if result.ok else 1


def _dag_validate(args: argparse.Namespace) -> int:
    if args.dag_file:
        result = run_phart_on_file(Path(args.dag_file), "validate", json_out=args.json)
    else:
        dag = _load_dag_arg(args)
        result = run_phart_on_dag(dag, "validate", json_out=args.json)
    return _emit_phart_result(result)


def _dag_chart(args: argparse.Namespace) -> int:
    if args.dag_file:
        result = run_phart_on_file(Path(args.dag_file), "chart")
    else:
        dag = _load_dag_arg(args)
        result = run_phart_on_dag(dag, "chart")
    return _emit_phart_result(result)


EXEC_PROFILES = {
    "oc-chutes-deepseek": "opencode_exec",
    "pi-chutes-kimi": "pi_exec",
    "pi-opencode-kimi": "pi_exec",
    "codex-gpt-5.5": "codex_exec",
    "codex-vision": "codex_exec",
    "kimi-k2.6": "kimi_exec",
    "kimi-k2.5": "kimi_exec",
    "kimi": "kimi_exec",
    "cursor-auto": "cursor_exec",
    "cursor-plan": "cursor_exec",
    "cursor-composer-2.5": "cursor_exec",
}

REASONING_EFFORTS = {"none", "low", "medium", "high"}
LEGACY_COMMANDS = {"exec", "dag", "harness", "image"}
PROMPT_WEASEL_WORDS = {
    "relevant",
    "appropriate",
    "comprehensive",
    "thorough",
    "important",
    "ensure",
    "consider",
    "properly",
    "meaningful",
    "high-quality",
    "as needed",
    "various",
    "leverage",
    "utilize",
}


def _default_api_key() -> str:
    return (
        os.environ.get("SCILLM_API_KEY")
        or os.environ.get("SCILLM_PROXY_KEY")
        or os.environ.get("SCILLM_MASTER_KEY")
        or os.environ.get("LITELLM_MASTER_KEY")
        or "sk-dev-proxy-123"
    )


def _normalize_project_model(selector: str) -> str:
    """Normalize project-agent model selectors into proxy model ids."""
    if selector.startswith("opencode/"):
        return "opencode-go/" + selector.removeprefix("opencode/")
    if selector.startswith("openai/"):
        return selector.removeprefix("openai/")
    if selector.startswith("chutes/"):
        return selector.removeprefix("chutes/")
    return selector


def _is_model_selector(value: str) -> bool:
    return "/" in value or value in {
        "gpt-5.5",
        "claude-sonnet-4-6",
        "gemini-flash",
        "moonshot-text",
        "oc-kimi",
        "oc-deepseek",
        "oc-glm",
        "oc-qwen",
    }


def _prompt_text_from_request(request: dict[str, Any]) -> str:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


def _lint_prompt_best_practices(prompt: str) -> dict[str, Any]:
    lower = prompt.lower()
    lines = prompt.splitlines()
    failures: list[dict[str, str]] = []

    if "# rationale" not in lower[:500]:
        failures.append(
            {
                "rule": "structure-rationale-header",
                "message": "Prompt is missing a # RATIONALE block near the top.",
            }
        )
    for required in ("purpose:", "consumer:", "why this matters:"):
        if required not in lower[:1000]:
            failures.append(
                {
                    "rule": "structure-rationale-header",
                    "message": f"Rationale block is missing {required.rstrip(':')}.",
                }
            )
    found_weasel = sorted(word for word in PROMPT_WEASEL_WORDS if word in lower)
    if found_weasel:
        failures.append(
            {
                "rule": "clarity-no-weasel-words",
                "message": "Banned vague words found: " + ", ".join(found_weasel),
            }
        )
    if not any(token in lower for token in ("return only", "output nothing but", "json object", "json array", "schema")):
        failures.append(
            {
                "rule": "specificity-name-the-format",
                "message": "Prompt does not name an exact output format or schema.",
            }
        )
    if "example" not in lower or not any(token in lower for token in ("expected output", "valid output", "input text", "input:")):
        failures.append(
            {
                "rule": "output-show-one-example",
                "message": "Prompt lacks a complete input/output example.",
            }
        )
    if not any(token in lower for token in ("valid values", "valid categories", "exactly one", "choose one of")) and any(
        token in lower for token in ("classify", "classification", "category", "severity")
    ):
        failures.append(
            {
                "rule": "grounding-vocabulary-control",
                "message": "Classification prompt lacks a closed vocabulary.",
            }
        )
    if not any(token in lower for token in ("do not include", "wrong", "invalid", "reject", "if no", "return []")):
        failures.append(
            {
                "rule": "output-rejection-criteria",
                "message": "Prompt does not define rejection criteria or empty-result behavior.",
            }
        )
    if not any(token in lower for token in ("validate", "validator", "pydantic", "regex", "must match", "checked by")):
        failures.append(
            {
                "rule": "testability-deterministic-check",
                "message": "Prompt does not define a deterministic check.",
            }
        )
    first_instruction = next((line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")), "")
    if first_instruction.lower().startswith(("you should", "it would be", "please try")):
        failures.append(
            {
                "rule": "clarity-imperative-voice",
                "message": "Prompt starts with weak/descriptive language instead of an imperative task.",
            }
        )
    return {"ok": not failures, "failure_count": len(failures), "failures": failures}


def _enforce_prompt_gate(prompt_items: list[tuple[str, str]], *, json_out: bool) -> bool:
    reports = []
    ok = True
    for item_id, prompt in prompt_items:
        report = _lint_prompt_best_practices(prompt)
        report["item_id"] = item_id
        reports.append(report)
        ok = ok and bool(report.get("ok"))
    if ok:
        return True
    payload = {"ok": False, "error": "PROMPT_GATE_FAIL", "reports": reports}
    _print_json_or_text(payload, json_out=True if json_out else True)
    return False


def _load_tools_file(path: str | None) -> list[dict[str, Any]] | None:
    if not path:
        return None
    loaded = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    tools = loaded.get("tools") if isinstance(loaded, dict) else loaded
    if not isinstance(tools, list) or not tools:
        raise ValueError("tools file must be a non-empty JSON array or object with tools[]")
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ValueError(f"tools[{index}] must be an object")
        if tool.get("type") != "function":
            raise ValueError(f"tools[{index}].type must be 'function'")
        fn = tool.get("function")
        if not isinstance(fn, dict):
            raise ValueError(f"tools[{index}].function must be an object")
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"tools[{index}].function.name is required")
        parameters = fn.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError(f"tools[{index}].function.parameters must be a JSON Schema object")
        if parameters.get("type") != "object":
            raise ValueError(f"tools[{index}].function.parameters.type must be 'object'")
    return tools


def _headers(args: argparse.Namespace) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {args.api_key}",
        "X-Caller-Skill": args.caller,
        "Content-Type": "application/json",
    }


def _message_text_from_response(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, indent=2)


def _load_batch_requests(path: Path, *, model: str) -> list[dict[str, Any]]:
    text = path.expanduser().read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        raw_items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        loaded = json.loads(text)
        if isinstance(loaded, dict) and isinstance(loaded.get("requests"), list):
            raw_items = loaded["requests"]
        elif isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
            raw_items = loaded["items"]
        elif isinstance(loaded, list):
            raw_items = loaded
        else:
            raise ValueError("batch file must be a JSON list, JSONL, or object with requests/items")

    requests: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if isinstance(item, str):
            request = {"messages": [{"role": "user", "content": item}]}
        elif isinstance(item, dict):
            request = dict(item)
            if "messages" not in request:
                prompt = request.get("prompt") or request.get("content") or request.get("text")
                if not isinstance(prompt, str) or not prompt:
                    raise ValueError(f"batch item {index} needs messages, prompt, content, or text")
                request["messages"] = [{"role": "user", "content": prompt}]
        else:
            raise ValueError(f"batch item {index} must be a string or object")
        request.setdefault("item_id", request.get("id") or f"item-{index}")
        request["model"] = request.get("model") or model
        requests.append(request)
    if not requests:
        raise ValueError("batch file has no requests")
    return requests


def _print_json_or_text(data: Any, *, json_out: bool) -> None:
    if json_out:
        sys.stdout.write(json.dumps(data, indent=2) + "\n")
    elif isinstance(data, str):
        sys.stdout.write(data.rstrip() + "\n")
    else:
        sys.stdout.write(json.dumps(data, indent=2) + "\n")


def _run_project_chat(args: argparse.Namespace, *, model: str, prompt: str, reasoning: str | None) -> int:
    if args.prompt_gate and not _enforce_prompt_gate([("prompt", prompt)], json_out=args.json):
        return 1
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if reasoning:
        payload["reasoning_effort"] = reasoning
    tools = _load_tools_file(args.tools)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = args.tool_choice
    timeout = float(args.timeout)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{args.base_url.rstrip('/')}/v1/chat/completions",
            headers=_headers(args),
            json=payload,
        )
    if response.status_code >= 400:
        _print_json_or_text(response.json() if response.content else response.text, json_out=True)
        return 1
    data = response.json()
    _print_json_or_text(data if args.json else _message_text_from_response(data), json_out=args.json)
    return 0


def _run_project_model_batch(
    args: argparse.Namespace,
    *,
    model: str,
    input_path: Path,
    reasoning: str | None,
) -> int:
    requests = _load_batch_requests(input_path, model=model)
    if args.prompt_gate:
        prompt_items = [(str(request.get("item_id") or request.get("id") or f"item-{idx}"), _prompt_text_from_request(request)) for idx, request in enumerate(requests)]
        if not _enforce_prompt_gate(prompt_items, json_out=args.json):
            return 1
    timeout = float(args.timeout)
    base_url = args.base_url.rstrip("/")
    headers = _headers(args)
    tools = _load_tools_file(args.tools)

    def run_one(index_and_request: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, request = index_and_request
        item_id = str(request.get("item_id") or request.get("id") or f"item-{index}")
        payload: dict[str, Any] = {
            "model": _normalize_project_model(str(request.get("model") or model)),
            "messages": request["messages"],
            "scillm_metadata": {
                **(request.get("scillm_metadata") if isinstance(request.get("scillm_metadata"), dict) else {}),
                "batch_id": f"scillm-cli-{input_path.stem}",
                "item_id": item_id,
            },
        }
        item_reasoning = request.get("reasoning_effort") or reasoning
        if item_reasoning:
            payload["reasoning_effort"] = str(item_reasoning).lower()
        item_tools = request.get("tools") if isinstance(request.get("tools"), list) else tools
        if item_tools:
            payload["tools"] = item_tools
            payload["tool_choice"] = str(request.get("tool_choice") or args.tool_choice)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{base_url}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
            if response.status_code >= 400:
                error_text = response.text
                error_body: Any = response.text
                try:
                    error_body = response.json()
                    if isinstance(error_body, dict):
                        error_text = json.dumps(error_body)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "item_id": item_id,
                    "index": index,
                    "model": payload["model"],
                    "status_code": response.status_code,
                    "error": error_text,
                    "request": payload,
                    "response": error_body,
                }
            data = response.json()
            content = _message_text_from_response(data)
            return {
                "ok": bool(content.strip()),
                "item_id": item_id,
                "index": index,
                "model": payload["model"],
                "served_model": data.get("model"),
                "content": content,
                "error": None if content.strip() else "empty_response_content",
                "request": payload,
                "response": data,
            }
        except Exception as exc:
            return {
                "ok": False,
                "item_id": item_id,
                "index": index,
                "model": payload["model"],
                "error": str(exc) or type(exc).__name__,
                "error_type": type(exc).__name__,
                "request": payload,
                "response": None,
            }

    max_workers = max(1, int(args.concurrency))
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_one, item) for item in enumerate(requests)]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            if args.json:
                sys.stdout.write(json.dumps(result) + "\n")
            elif result.get("ok"):
                sys.stdout.write(f"{result.get('item_id')}: {str(result.get('content') or '').rstrip()}\n")
            else:
                sys.stdout.write(f"{result.get('item_id')}: ERROR {result.get('error')}\n")

    ok = len(results) == len(requests) and all(bool(result.get("ok")) for result in results)
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "type": "scillm_cli_batch_summary",
                    "ok": ok,
                    "model": model,
                    "input": str(input_path),
                    "expected_items": len(requests),
                    "terminal_items": len(results),
                }
            )
            + "\n"
        )
    return 0 if ok else 1


def _run_project_chutes_batch(args: argparse.Namespace, *, model: str, input_path: Path) -> int:
    requests = _load_batch_requests(input_path, model=model)
    if args.prompt_gate:
        prompt_items = [(str(request.get("item_id") or request.get("id") or f"item-{idx}"), _prompt_text_from_request(request)) for idx, request in enumerate(requests)]
        if not _enforce_prompt_gate(prompt_items, json_out=args.json):
            return 1
    payload = {
        "requests": requests,
        "concurrency": args.concurrency,
        "wall_time_s": args.wall_time_s,
    }
    events: list[dict[str, Any]] = []
    with httpx.Client(timeout=float(args.wall_time_s) + 30.0) as client:
        with client.stream(
            "POST",
            f"{args.base_url.rstrip('/')}/v1/scillm/chutes/batch",
            headers=_headers(args),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                try:
                    _print_json_or_text(response.json(), json_out=True)
                except Exception:
                    sys.stdout.write(response.text + "\n")
                return 1
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if not raw:
                    continue
                event = json.loads(raw)
                events.append(event)
                if args.json:
                    sys.stdout.write(json.dumps(event) + "\n")
                elif "content" in event:
                    sys.stdout.write(f"{event.get('item_id')}: {event.get('content', '').rstrip()}\n")
    terminal = [event for event in events if "item_id" in event and "attempts" in event and "content" in event]
    ok = len(terminal) == len(requests) and all(bool(event.get("ok")) for event in terminal)
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "type": "scillm_cli_batch_summary",
                    "ok": ok,
                    "model": model,
                    "input": str(input_path),
                    "expected_items": len(requests),
                    "terminal_items": len(terminal),
                }
            )
            + "\n"
        )
    return 0 if ok else 1


def _run_project_agent(args: argparse.Namespace, *, prompt: str) -> int:
    payload: dict[str, Any] = {
        "agent": args.profile,
        "prompt": prompt,
        "wait": True,
        "timeout_s": args.timeout,
        # The delegate works where the caller stands: artifact instructions
        # like "write ./receipt.json" must resolve against the caller's cwd
        # (issue #19), not an unrelated server default directory.
        "cwd": os.getcwd(),
    }
    if args.model:
        # The serve surface speaks native opencode ids (opencode/<model>);
        # opencode-go/ is the proxy CHAT prefix and 500s inside opencode.
        model = args.model
        if model.startswith("opencode-go/"):
            model = "opencode/" + model.removeprefix("opencode-go/")
        elif not model.startswith("opencode/"):
            model = _normalize_project_model(model)
        payload["model"] = model
    with httpx.Client(timeout=float(args.timeout) + 30.0) as client:
        response = client.post(
            f"{args.base_url.rstrip('/')}/v1/scillm/opencode/runs",
            headers=_headers(args),
            json=payload,
        )
    if response.status_code >= 400:
        _print_json_or_text(response.json() if response.content else response.text, json_out=True)
        return 1
    data = response.json()
    if args.json:
        _print_json_or_text(data, json_out=True)
    else:
        text = str(data.get("assistant_text") or data.get("result", {}).get("assistant_text") or "")
        _print_json_or_text(text or data, json_out=False)

    # Delegate contract (issue #19): never exit 0 with a success-looking
    # payload unless the run actually completed and any required artifacts
    # exist on disk.
    status = str(data.get("status") or data.get("state") or "")
    if status != "completed":
        sys.stderr.write(
            json.dumps({
                "schema": "scillm.cli.delegate_blocked.v1",
                "ok": False,
                "reason": f"delegate run terminal status is {status!r}, not 'completed'",
                "run_id": data.get("run_id"),
                "terminal_blocker": data.get("terminal_blocker"),
            }) + "\n"
        )
        return 2
    missing = [p for p in (getattr(args, "require_artifact", None) or []) if not Path(p).expanduser().is_file()]
    if missing:
        sys.stderr.write(
            json.dumps({
                "schema": "scillm.cli.delegate_blocked.v1",
                "ok": False,
                "reason": "delegate reported completed but required artifact files are absent",
                "missing_artifacts": missing,
                "run_id": data.get("run_id"),
            }) + "\n"
        )
        return 3
    return 0


def _build_project_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scillm",
        description="Project-agent paved path: model prompt, model batch-file, or agent task.",
    )
    parser.add_argument("--base-url", default=os.environ.get("SCILLM_BASE_URL", "http://127.0.0.1:4001"))
    parser.add_argument("--api-key", default=_default_api_key())
    parser.add_argument("--caller", default=os.environ.get("SCILLM_CALLER_SKILL", "scillm-cli"))
    parser.add_argument("--json", action="store_true", help="print raw JSON/NDJSON instead of extracted text")
    parser.add_argument("--prompt-gate", action="store_true", help="reject non-agent prompts that fail best-practices-prompt checks")
    parser.add_argument("--tools", help="OpenAI-style tools JSON file to pass to non-agent model calls")
    parser.add_argument("--tool-choice", choices=["auto", "none", "required"], default="auto")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("SCILLM_TIMEOUT", "300")))
    parser.add_argument("--profile", default="build", help="agent profile for `scillm agent`")
    parser.add_argument("--model", default=None, help="model override for `scillm agent`")
    parser.add_argument(
        "--require-artifact",
        action="append",
        default=[],
        dest="require_artifact",
        help="fail closed (exit 3) if this file does not exist after a completed agent run; repeatable",
    )
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("SCILLM_BATCH_CONCURRENCY", "3")))
    parser.add_argument("--wall-time-s", type=float, default=float(os.environ.get("SCILLM_BATCH_WALL_TIME_S", "600")))
    parser.add_argument("words", nargs="*")
    return parser


def _project_main(argv: list[str]) -> int:
    parser = _build_project_parser()
    # parse_known_args: prompt text after interleaved options must land back
    # in words (argparse cannot resume a consumed nargs="*" positional).
    args, extra = parser.parse_known_args(argv)
    words = list(args.words) + [w for w in extra if not w.startswith("--")]
    unknown_opts = [w for w in extra if w.startswith("--")]
    if unknown_opts:
        parser.error(f"unrecognized arguments: {' '.join(unknown_opts)}")
    if not words:
        parser.print_usage(sys.stderr)
        return 2

    if words[0] == "prompt":
        words.pop(0)
        if not words or words[0] != "check":
            raise ValueError("usage: scillm prompt check <prompt-file>")
        words.pop(0)
        if len(words) != 1:
            raise ValueError("usage: scillm prompt check <prompt-file>")
        prompt_path = Path(words[0]).expanduser()
        report = _lint_prompt_best_practices(prompt_path.read_text(encoding="utf-8"))
        report["prompt_file"] = str(prompt_path)
        _print_json_or_text(report, json_out=True)
        return 0 if report.get("ok") else 1

    if words[0] == "tools":
        words.pop(0)
        if not words or words[0] != "check":
            raise ValueError("usage: scillm tools check <tools-json>")
        words.pop(0)
        if len(words) != 1:
            raise ValueError("usage: scillm tools check <tools-json>")
        tools_path = Path(words[0]).expanduser()
        tools = _load_tools_file(str(tools_path))
        _print_json_or_text(
            {
                "ok": True,
                "tools_file": str(tools_path),
                "tool_count": len(tools or []),
                "tool_names": [tool["function"]["name"] for tool in tools or []],
            },
            json_out=True,
        )
        return 0

    if words[0] == "chat":
        words.pop(0)

    if words and words[0] == "agent":
        words.pop(0)
        while words and words[0].startswith("--"):
            option = words.pop(0)
            if option == "--profile" and words:
                args.profile = words.pop(0)
            elif option == "--model" and words:
                args.model = words.pop(0)
            elif option == "--require-artifact" and words:
                args.require_artifact.append(words.pop(0))
            else:
                raise ValueError(f"unknown or incomplete agent option: {option}")
        if words and _is_model_selector(words[0]):
            args.model = words.pop(0)
        if not words:
            raise ValueError("scillm agent requires a task prompt")
        return _run_project_agent(args, prompt=" ".join(words))

    selector = words.pop(0)
    if not _is_model_selector(selector):
        words.insert(0, selector)
        selector = os.environ.get("SCILLM_MODEL", "gpt-5.5")
    model = _normalize_project_model(selector)

    reasoning = None
    if words and words[0].lower() in REASONING_EFFORTS:
        reasoning = words.pop(0).lower()
    if not words:
        raise ValueError("scillm model call requires a prompt or batch file")

    if len(words) == 1 and Path(words[0]).expanduser().is_file():
        input_path = Path(words[0]).expanduser()
        if input_path.suffix.lower() not in {".json", ".jsonl"}:
            prompt = input_path.read_text(encoding="utf-8")
            return _run_project_chat(args, model=model, prompt=prompt, reasoning=reasoning)
        if selector.startswith("chutes/") or model.startswith(("deepseek-ai/", "Qwen/", "moonshotai/")):
            return _run_project_chutes_batch(args, model=model, input_path=input_path)
        return _run_project_model_batch(args, model=model, input_path=input_path, reasoning=reasoning)

    return _run_project_chat(args, model=model, prompt=" ".join(words), reasoning=reasoning)


def build_exec_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Build the /v1/scillm/exec payload for a headless exec run."""
    if args.profile not in EXEC_PROFILES:
        raise ValueError(
            f"unsupported scillm exec profile {args.profile!r}; "
            f"use one of {sorted(EXEC_PROFILES)}"
        )
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    if not prompt:
        raise ValueError("scillm exec requires --prompt, --prompt-file, or stdin")

    metadata: dict[str, Any] = {}
    if args.allow_write:
        metadata["allow_write_paths"] = args.allow_write
    if args.skills:
        skills: list[str] = []
        for item in args.skills:
            skills.extend(part.strip() for part in item.split(",") if part.strip())
        metadata["skills"] = skills
    if args.cursor_model:
        metadata["cursor_model"] = args.cursor_model
    if args.cursor_force:
        metadata["cursor_force"] = True
    codex_model = getattr(args, "codex_model", None)
    if codex_model:
        metadata["codex_model"] = codex_model

    payload: dict[str, Any] = {
        "id": args.node_id,
        "type": EXEC_PROFILES[args.profile],
        "model": args.profile,
        "sandbox": args.sandbox,
        "cwd": str(Path(args.cwd).expanduser().resolve()),
        "node_goal": args.node_goal,
        "prompt": prompt,
        "timeout_s": args.timeout,
        "idle_timeout_s": args.idle_timeout,
        "metadata": metadata,
    }
    reasoning_effort = getattr(args, "reasoning_effort", None)
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if args.run_id:
        payload["run_id"] = args.run_id
    return payload


def _exec(args: argparse.Namespace) -> int:
    if args.prompt is None and not args.prompt_file and not sys.stdin.isatty():
        args.prompt = sys.stdin.read()
    payload = build_exec_payload(args)
    base_url = args.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "X-Caller-Skill": args.caller,
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=args.timeout + 30.0) as client:
        response = client.post(f"{base_url}/v1/scillm/exec", headers=headers, json=payload)
    sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")
    return 0 if response.status_code < 400 else 1




def _harness_turn(args: argparse.Namespace) -> int:
    from scillm.harness.dag_turn_loop import run_dag_turn_iteration

    message = args.message
    if args.message_file:
        message = Path(args.message_file).expanduser().read_text(encoding="utf-8")
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    if not message:
        raise ValueError("harness turn requires --message, --message-file, or stdin")

    result = run_dag_turn_iteration(
        args.thread_id,
        message,
        model=args.model,
        scillm_base_url=args.base_url,
        scillm_api_key=args.api_key,
        seed_if_empty=not args.no_seed,
        seed_dag=_load_seed_dag_arg(args),
    )
    payload = {
        "turn_key": result.turn_key,
        "thread_id": result.thread_id,
        "dag_id": result.dag_id,
        "node_count": result.node_count,
        "model_served": result.model_served,
        "turn_status": result.turn_status,
        "terminal_reason": result.terminal_reason,
        "node_results_summary": result.node_results_summary,
    }
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if result.turn_status in {None, "completed", "ok"} else 1


def _harness_semantic(args: argparse.Namespace) -> int:
    from scillm.harness.semantic_dag_run import run_semantic_dag

    payload = _load_dag_arg(args)
    if args.cwd:
        payload["cwd"] = str(Path(args.cwd).expanduser().resolve())
    try:
        receipt = run_semantic_dag(
            payload,
            write_harness_turn=not args.no_harness_turn,
            thread_id=args.thread_id,
        )
    except Exception as exc:
        err_receipt = {
            "schema": "scillm.harness_dag_run.v1",
            "status": "failed",
            "overall_status": "FAIL",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "dag_id": payload.get("dag_id") or "",
            "graph_id": payload.get("graph_id") or "",
        }
        sys.stdout.write(json.dumps(err_receipt, indent=2) + "\n")
        return 1
    sys.stdout.write(json.dumps(receipt.as_dict(), indent=2) + "\n")
    overall = (receipt.smoke_enrichment or {}).get("overall_status")
    if overall == "PASS":
        return 0
    if overall:
        return 1
    return 0 if receipt.status == "completed" else 1


def _terminal_status_from_loop(result: dict[str, Any]) -> str:
    """Map loop result to operator-facing terminal_status vocabulary."""
    if result.get("human_required"):
        return "human_required"
    if result.get("session_complete"):
        return "complete"
    reason = str(result.get("terminal_reason") or "")
    if reason.startswith("human_") or "human_required" in reason:
        return "human_required"
    return "blocked"


def _write_harness_loop_output(result: dict[str, Any], *, watch: bool) -> None:
    if not watch:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(
        json.dumps(
            {
                "watch_event": "watch_started",
                "entrypoint": result.get("entrypoint", "scillm harness loop"),
                "thread_id": result.get("thread_id"),
                "scenario": result.get("scenario"),
            }
        )
        + "\n"
    )
    for event in result.get("progress_events") or []:
        if isinstance(event, dict):
            sys.stdout.write(json.dumps({"watch_event": "progress", **event}) + "\n")
    sys.stdout.write(
        json.dumps(
            {
                "watch_event": "final",
                "terminal_status": result.get("terminal_status"),
                "session_complete": result.get("session_complete"),
                "pass": result.get("pass"),
                "result": result,
            }
        )
        + "\n"
    )


def _harness_loop(args: argparse.Namespace) -> int:
    from scillm.harness.context_pack import check_memory_reachable
    from scillm.harness.dag_turn_loop import run_dag_turn_loop
    from scillm.harness.planner_gate_messages import (
        is_fail_closed_terminal,
        resolve_planner_gate_message,
    )
    from scillm.harness.product_scenarios import resolve_product_scenario, run_product_scenario

    watch = bool(getattr(args, "watch", False))
    mem = check_memory_reachable()
    if not mem.get("memory_reachable"):
        blocked = {
            "entrypoint": "scillm harness loop",
            "session_complete": False,
            "terminal_status": "blocked",
            "terminal_reason": "memory_unreachable",
            "memory_reachable": False,
            "memory_health": mem,
            "pass": False,
        }
        _write_harness_loop_output(blocked, watch=watch)
        return 1

    messages = list(args.message or [])
    if args.messages_file:
        for line in Path(args.messages_file).expanduser().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                messages.append(line)
    if not messages and not sys.stdin.isatty():
        for line in sys.stdin.read().splitlines():
            line = line.strip()
            if line:
                messages.append(line)
    if not messages:
        raise ValueError("harness loop requires --message, --messages-file, or stdin lines")

    seed_dag = _load_seed_dag_arg(args)
    if seed_dag is not None:
        result = run_dag_turn_loop(
            args.thread_id,
            messages,
            model=args.model,
            scillm_base_url=args.base_url,
            scillm_api_key=args.api_key,
            live_planner_turn1=False,
            strict_open_nlp_policy=False,
            seed_dag=seed_dag,
        )
        result.setdefault("entrypoint", "scillm harness loop")
        result.setdefault("memory_reachable", True)
        result.setdefault("terminal_status", _terminal_status_from_loop(result))
        _write_harness_loop_output(result, watch=watch)
        return 0 if result.get("session_complete") else 1

    if len(messages) == 1:
        from scillm.harness.out_of_scope import check_out_of_scope_message

        oos = check_out_of_scope_message(messages[0])
        if oos:
            blocked = {
                "entrypoint": "scillm harness loop",
                "session_complete": False,
                "scenario": "out_of_scope_request",
                "natural_language_prompt_used": True,
                "caller_subprocess_only": True,
                "memory_reachable": True,
                "pass": True,
                **oos,
            }
            _write_harness_loop_output(blocked, watch=watch)
            return 1

    if len(messages) == 1:
        scenario_id = resolve_product_scenario(messages[0])
        if scenario_id:
            result = run_product_scenario(
                scenario_id,
                args.thread_id,
                messages[0],
                model=args.model,
                scillm_base_url=args.base_url,
                scillm_api_key=args.api_key,
            )
            if "terminal_status" not in result:
                result["terminal_status"] = _terminal_status_from_loop(result)
            result["memory_reachable"] = True
            _write_harness_loop_output(result, watch=watch)
            return 0 if result.get("session_complete") else 1

        gate_kind = resolve_planner_gate_message(messages[0])
        if gate_kind:
            loop_result = run_dag_turn_loop(
                args.thread_id,
                messages,
                model=args.model,
                scillm_base_url=args.base_url,
                scillm_api_key=args.api_key,
                live_planner_turn1=True,
            )
            iteration = (loop_result.get("iterations") or [{}])[0]
            model_served = iteration.get("model_served")
            terminal_reason = iteration.get("terminal_reason")
            turn_status = iteration.get("turn_status")
            if gate_kind == "success":
                gate_pass = bool(
                    loop_result.get("session_complete")
                    and model_served
                    and iteration.get("node_count", 0) >= 2
                )
            else:
                node_summary = iteration.get("node_results_summary") or {}
                gate_pass = bool(
                    not loop_result.get("session_complete")
                    and model_served
                    and (
                        node_summary.get("policy_probe_det") == "blocked"
                        or (
                            turn_status == "failed"
                            and is_fail_closed_terminal(terminal_reason)
                        )
                        or is_fail_closed_terminal(terminal_reason)
                    )
                )
            result = {
                **loop_result,
                "entrypoint": "scillm harness loop",
                "scenario": "phase10_generic_planner",
                "gate_kind": gate_kind,
                "planner_turn1_live": True,
                "natural_language_prompt_used": True,
                "caller_subprocess_only": True,
                "pass": gate_pass,
                "memory_reachable": True,
                "terminal_status": _terminal_status_from_loop({**loop_result, "session_complete": loop_result.get("session_complete")}),
            }
            _write_harness_loop_output(result, watch=watch)
            return 0 if gate_pass else 1

        from scillm.harness.open_nlp import blocked_open_nlp_payload, classify_open_nlp_message

        classification = classify_open_nlp_message(messages[0])
        if classification.decision == "blocked":
            blocked = blocked_open_nlp_payload(message=messages[0], classification=classification)
            blocked["memory_reachable"] = True
            _write_harness_loop_output(blocked, watch=watch)
            return 1

    result = run_dag_turn_loop(
        args.thread_id,
        messages,
        model=args.model,
        scillm_base_url=args.base_url,
        scillm_api_key=args.api_key,
        live_planner_turn1=len(messages) == 1 and _load_seed_dag_arg(args) is None,
        strict_open_nlp_policy=len(messages) == 1,
        seed_dag=_load_seed_dag_arg(args),
    )
    result.setdefault("entrypoint", "scillm harness loop")
    result.setdefault("memory_reachable", True)
    result.setdefault("terminal_status", _terminal_status_from_loop(result))
    _write_harness_loop_output(result, watch=watch)
    return 0 if result.get("session_complete") else 1


def _image_generate(args: argparse.Namespace) -> int:
  """Delegate to scripts/generate_image.py for deterministic image termination events."""
  repo_root = Path(__file__).resolve().parents[2]
  script = repo_root / "scripts" / "generate_image.py"
  if not script.is_file():
    raise FileNotFoundError(f"missing image generator: {script}")
  cmd = [
      sys.executable,
      str(script),
      "--prompt-file",
      str(Path(args.prompt_file).expanduser()),
      "--out",
      str(Path(args.out).expanduser()),
      "--auth",
      args.auth,
      "--model",
      args.model,
      "--quality",
      args.quality,
      "--size",
      args.size,
      "--base-url",
      args.base_url.rstrip("/"),
      "--master-key",
      args.api_key,
      "--caller-skill",
      args.caller,
      "--timeout-s",
      str(args.timeout),
      "--json",
  ]
  if args.receipt:
    cmd.extend(["--receipt", str(Path(args.receipt).expanduser())])
  if args.cwd:
    cmd.extend(["-C", str(Path(args.cwd).expanduser())])
  if args.events_out:
    cmd.extend(["--events-out", str(Path(args.events_out).expanduser())])
  completed = subprocess.run(cmd, check=False)
  return int(completed.returncode)

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scillm")
    subparsers = parser.add_subparsers(dest="command", required=True)

    exec_parser = subparsers.add_parser("exec", help="run a bounded scillm exec worker")
    exec_parser.add_argument("profile", choices=sorted(EXEC_PROFILES))
    exec_parser.add_argument("--prompt", help="prompt text; stdin is used when omitted")
    exec_parser.add_argument("--prompt-file", help="read prompt text from a file")
    exec_parser.add_argument("--run-id")
    exec_parser.add_argument("--node-id", default="scillm_exec")
    exec_parser.add_argument("--node-goal", default="Run a bounded worker through scillm exec.")
    exec_parser.add_argument("--cwd", default=os.getcwd())
    exec_parser.add_argument("--sandbox", choices=["read-only", "workspace-write"], default="read-only")
    exec_parser.add_argument(
        "--allow-write",
        action="append",
        default=[],
        help="relative file, directory, or glob allowed to change; repeatable and required for workspace-write",
    )
    exec_parser.add_argument(
        "--skills",
        action="append",
        default=[],
        help="harness-selected skill directory names; materialized under .scillm/cursor-headless/",
    )
    exec_parser.add_argument(
        "--cursor-model",
        help="override Cursor --model flag (default comes from the exec profile)",
    )
    exec_parser.add_argument(
        "--cursor-force",
        action="store_true",
        help="pass --force to the Cursor agent (ignored for cursor-plan)",
    )
    exec_parser.add_argument(
        "--codex-model",
        help="override Codex --model flag (default comes from the exec profile)",
    )
    exec_parser.add_argument(
        "--reasoning-effort",
        help="Codex reasoning effort forwarded as -c reasoning.effort=...",
    )
    exec_parser.add_argument("--timeout", type=float, default=900.0)
    exec_parser.add_argument("--idle-timeout", type=float, default=300.0)
    exec_parser.add_argument("--base-url", default=os.environ.get("SCILLM_BASE_URL", "http://127.0.0.1:4001"))
    exec_parser.add_argument("--api-key", default=os.environ.get("SCILLM_PROXY_KEY", "sk-dev-proxy-123"))
    exec_parser.add_argument("--caller", default="scillm-cli-exec")
    exec_parser.set_defaults(func=_exec)

    dag_parser = subparsers.add_parser("dag", help="validate/chart DAG JSON via $phart-dag-chart")
    dag_sub = dag_parser.add_subparsers(dest="dag_command", required=True)

    dag_validate = dag_sub.add_parser("validate", help="validate DAG structure")
    dag_validate.add_argument("dag_file", nargs="?", help="path to dag.json")
    dag_validate.add_argument("--dag-json", help="inline DAG JSON string")
    dag_validate.add_argument("--json", action="store_true", help="machine-readable validation report")
    dag_validate.set_defaults(func=_dag_validate)

    dag_chart = dag_sub.add_parser("chart", help="render PHART ASCII decision tree")
    dag_chart.add_argument("dag_file", nargs="?", help="path to dag.json")
    dag_chart.add_argument("--dag-json", help="inline DAG JSON string")
    dag_chart.set_defaults(func=_dag_chart)


    harness_parser = subparsers.add_parser("harness", help="memory-first harness turn loop")
    harness_sub = harness_parser.add_subparsers(dest="harness_command", required=True)

    harness_turn = harness_sub.add_parser("turn", help="one DAG turn (recall → plan → exec → memory)")
    harness_turn.add_argument("--thread-id", required=True, help="stable harness thread id")
    harness_turn.add_argument("--message", help="user turn text")
    harness_turn.add_argument("--message-file", help="read user turn text from file")
    harness_turn.add_argument("--model", default=os.environ.get("SCILLM_HARNESS_MODEL", "oc-kimi"))
    harness_turn.add_argument("--no-seed", action="store_true", help="fail if no prior dag chart exists")
    harness_turn.add_argument("--seed-dag-file", help="harness_turn.dag.v1 JSON for first empty turn")
    harness_turn.add_argument("--seed-dag-json", help="inline harness_turn.dag.v1 JSON for first empty turn")
    harness_turn.add_argument("--base-url", default=os.environ.get("SCILLM_BASE_URL", "http://127.0.0.1:4001"))
    harness_turn.add_argument("--api-key", default=os.environ.get("SCILLM_PROXY_KEY", "sk-dev-proxy-123"))
    harness_turn.set_defaults(func=_harness_turn)

    harness_semantic = harness_sub.add_parser("semantic", help="run a harness_turn.dag.v1 fixture end-to-end")
    harness_semantic.add_argument("dag_file", nargs="?", help="path to harness_turn.dag.v1 JSON")
    harness_semantic.add_argument("--dag-json", help="inline DAG JSON")
    harness_semantic.add_argument("--thread-id", required=True)
    harness_semantic.add_argument("--cwd", help="working directory for local_command nodes")
    harness_semantic.add_argument("--base-url", default=os.environ.get("SCILLM_BASE_URL", "http://127.0.0.1:4001"))
    harness_semantic.add_argument("--api-key", default=os.environ.get("SCILLM_PROXY_KEY", "sk-dev-proxy-123"))
    harness_semantic.add_argument(
        "--no-harness-turn",
        action="store_true",
        help="Skip memory harness-turn upsert (smoke runs)",
    )
    harness_semantic.set_defaults(func=_harness_semantic)

    harness_loop = harness_sub.add_parser("loop", help="multi-turn harness session")
    harness_loop.add_argument("--thread-id", required=True)
    harness_loop.add_argument("--message", action="append", default=[], help="repeatable user messages in order")
    harness_loop.add_argument("--messages-file", help="one message per line")
    harness_loop.add_argument("--model", default=os.environ.get("SCILLM_HARNESS_MODEL", "oc-kimi"))
    harness_loop.add_argument("--base-url", default=os.environ.get("SCILLM_BASE_URL", "http://127.0.0.1:4001"))
    harness_loop.add_argument("--api-key", default=os.environ.get("SCILLM_PROXY_KEY", "sk-dev-proxy-123"))
    harness_loop.add_argument("--watch", action="store_true", help="emit line-delimited progress_events followed by final result")
    harness_loop.add_argument("--seed-dag-file", help="harness_turn.dag.v1 JSON for first empty turn")
    harness_loop.add_argument("--seed-dag-json", help="inline harness_turn.dag.v1 JSON for first empty turn")
    harness_loop.set_defaults(func=_harness_loop)

    image_parser = subparsers.add_parser(
        "image",
        help="generate an image with explicit scillm.image.* termination events",
    )
    image_sub = image_parser.add_subparsers(dest="image_command", required=True)
    image_gen = image_sub.add_parser("generate", help="prompt file → PNG + receipt")
    image_gen.add_argument("--prompt-file", required=True)
    image_gen.add_argument("--out", required=True)
    image_gen.add_argument("--receipt")
    image_gen.add_argument("--auth", choices=["codex-oauth", "openai-api-key"], default="openai-api-key")
    image_gen.add_argument("-C", "--cwd", default=os.getcwd())
    image_gen.add_argument("--events-out")
    image_gen.add_argument("--model", default=os.environ.get("SCILLM_IMAGE_MODEL", "gpt-image-2"))
    image_gen.add_argument("--quality", default=os.environ.get("SCILLM_IMAGE_QUALITY", "high"))
    image_gen.add_argument("--size", default=os.environ.get("SCILLM_IMAGE_SIZE", "auto"))
    image_gen.add_argument("--base-url", default=os.environ.get("SCILLM_BASE_URL", "http://127.0.0.1:4001"))
    image_gen.add_argument("--api-key", default=os.environ.get("SCILLM_PROXY_KEY", "sk-dev-proxy-123"))
    image_gen.add_argument("--caller", default="scillm-cli-image")
    image_gen.add_argument("--timeout", type=float, default=300.0)
    image_gen.set_defaults(func=_image_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] not in LEGACY_COMMANDS:
        try:
            return _project_main(argv)
        except Exception as exc:
            print(f"scillm: error: {exc}", file=sys.stderr)
            return 2
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"scillm: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
