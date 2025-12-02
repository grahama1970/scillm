import asyncio, os, json, sys
from scillm import parallel_acompletions

async def main():
    base = os.environ.get("CHUTES_API_BASE")
    key = os.environ.get("CHUTES_API_KEY")
    model = os.environ.get("CHUTES_MODEL_ID") or os.environ.get("CHUTES_TEXT_MODEL")
    if not base or not key or not model:
        print("ENV_MISSING CHUTES_API_BASE/CHUTES_API_KEY/CHUTES_MODEL_ID", file=sys.stderr)
        sys.exit(12)

    reqs = [
        {
            "messages": [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "Return only {\"ok\":true} as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 64,
            "temperature": 0,
            "model": model,
        },
        {
            "messages": [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "Return only {\"n\":42} as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 64,
            "temperature": 0,
            "model": model,
        },
    ]

    resps = await parallel_acompletions(
        reqs,
        api_base=base,
        api_key=key,
        custom_llm_provider="openai_like",
        concurrency=2,
        tenacious=False,
        timeout=20,
        wall_time_s=60,
        response_format={"type": "json_object"},
        default_temperature=0.0,
        default_max_tokens=64,
    )

    ok = True
    for r in resps:
        err = r.get("error")
        status = r.get("status")
        content = (r.get("content") or "")[:120]
        print(json.dumps({"index": r.get("index"), "error": err, "status": status, "content": content}, ensure_ascii=False))
        if err:
            ok = False
    if not ok:
        sys.exit(1)

if __name__ == "__main__":
    os.environ.setdefault("SCILLM_JSON_STRICT", "1")
    asyncio.run(main())
