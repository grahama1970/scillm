from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/v1/models")
async def models(request: Request):
    # visible model when auth OK
    key = request.headers.get("x-api-key")
    if not key or key != "good":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"data": [{"id": "demo-model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    key = request.headers.get("x-api-key")
    if not key or key != "good":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    content = "OK"
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": body.get("model", "demo-model"),
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
    }

