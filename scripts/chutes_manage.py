#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

from scillm.extras.chutes import ChuteSession, close, ensure, infer


def main() -> int:
    ap = argparse.ArgumentParser(description="Manage and use Chutes with SciLLM")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="Build/deploy (ensure) a chute")
    sp.add_argument("name")
    sp.add_argument("--api-key", dest="api_key")
    sp.add_argument("--template", dest="template")
    sp.add_argument("--accept-fee", action="store_true")

    si = sub.add_parser("infer", help="Run one chat completion on a chute")
    si.add_argument("name")
    si.add_argument("--api-key", dest="api_key")
    si.add_argument("--model", required=True)
    si.add_argument("--prompt", required=True)
    si.add_argument("--json", action="store_true", dest="json_mode")
    si.add_argument("--ttl-sec", type=float, default=None)
    si.add_argument("--ephemeral", action="store_true")
    si.add_argument("--max-tokens", type=int, default=None)
    si.add_argument("--temperature", type=float, default=None)
    si.add_argument("--top-p", type=float, default=None)
    si.add_argument("--seed", type=int, default=None)

    sd = sub.add_parser("delete", help="Delete a chute")
    sd.add_argument("name")

    args = ap.parse_args()

    if args.cmd == "start":
        ch = ensure(args.name, api_key=args.api_key, template=args.template, accept_fee=args.accept_fee)
        print(json.dumps({"name": ch.name, "base_url": ch.base_url}))
        return 0
    if args.cmd == "infer":
        ch = ensure(args.name, api_key=args.api_key, ttl_sec=args.ttl_sec)
        rf = {"type": "json_object"} if args.json_mode else None
        out = infer(
            ch,
            model=args.model,
            messages=[{"role": "user", "content": args.prompt}],
            response_format=rf,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            seed=args.seed,
        )
        print(json.dumps(out))
        if args.ephemeral:
            close(args.name)
        return 0
    if args.cmd == "delete":
        close(args.name)
        print(json.dumps({"deleted": args.name}))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
