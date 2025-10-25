#!/usr/bin/env python3
from __future__ import annotations

import os
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/v1/models")
def list_models(request: Request):
    # Accept either x-api-key or raw Authorization. Reject Bearer.
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    xk = request.headers.get("x-api-key")
    if isinstance(auth, str) and auth.lower().startswith("bearer ") and not xk:
        return JSONResponse({"error": "Bearer not accepted"}, status_code=401)
    models = {
        "object": "list",
        "data": [
            {"id": "mock-text-001", "object": "model"},
            {"id": "mock-vlm-001", "object": "model"},
        ],
    }
    return JSONResponse(models)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    # Same auth rule as /models
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    xk = request.headers.get("x-api-key")
    if isinstance(auth, str) and auth.lower().startswith("bearer ") and not xk:
        return JSONResponse({"error": "Missing or invalid auth header"}, status_code=401)

    content = "{\"ok\":true}"
    resp = {
        "id": "cmpl-mock",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}},
        ],
    }
    return JSONResponse(resp)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("CHUTES_MOCK_PORT", "18093"))
    uvicorn.run(app, host="127.0.0.1", port=port)

