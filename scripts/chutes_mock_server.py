#!/usr/bin/env python3
"""
Local mock for an OpenAI-compatible gateway that rejects Bearer and
requires x-api-key (or raw Authorization).

Endpoints:
  GET  /v1/models               → 401 if Bearer; 200 when x-api-key or raw Authorization
  POST /v1/chat/completions     → 200 when x-api-key or raw Authorization; returns OpenAI-style JSON

Run:
  uvicorn scripts.chutes_mock_server:app --port 18089 --host 127.0.0.1
"""
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()

def _auth_style(req: Request) -> str:
    auth = req.headers.get("authorization")
    xkey = req.headers.get("x-api-key")
    if xkey:
        return "x-api-key"
    if auth:
        if auth.lower().startswith("bearer "):
            return "bearer"
        return "raw"
    return "none"


@app.get("/v1/models")
async def models(request: Request):
    style = _auth_style(request)
    if style == "bearer":
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid token."}})
    if style in ("x-api-key", "raw"):
        return JSONResponse(status_code=200, content={"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
    return JSONResponse(status_code=401, content={"error": {"message": "Missing credentials"}})


@app.post("/v1/chat/completions")
async def chat(request: Request):
    style = _auth_style(request)
    body = await request.json()
    if style not in ("x-api-key", "raw"):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid token."}})
    content = "{\"ok\":true}"
    return JSONResponse(
        status_code=200,
        content={
            "id": "cmpl-stub",
            "object": "chat.completion",
            "model": body.get("model", "stub-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )

