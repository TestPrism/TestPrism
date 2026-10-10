"""OpenAI /v1/chat/completions -> copilot-api /v1/responses shim.

Listens on loopback, accepts chat-completions requests, forwards them to
copilot-api's /v1/responses endpoint, maps the response back to the
chat-completions schema litellm expects.

Uses Starlette directly (skips FastAPI to avoid a fastapi 0.124 /
starlette 1.0 incompatibility in the current venv).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
import uuid
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        text_type = "output_text" if role == "assistant" else "input_text"
        image_allowed = role != "assistant"
        content = m.get("content", "")
        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for c in content:
                if isinstance(c, dict):
                    ctype = c.get("type")
                    if ctype in ("text", "input_text", "output_text"):
                        parts.append({"type": text_type, "text": c.get("text", "")})
                    elif ctype == "image_url" and image_allowed:
                        url = c.get("image_url", {})
                        if isinstance(url, dict):
                            url = url.get("url", "")
                        parts.append({"type": "input_image", "image_url": url})
                    else:
                        parts.append({"type": text_type, "text": json.dumps(c)})
                else:
                    parts.append({"type": text_type, "text": str(c)})
            out.append({"role": role, "content": parts})
        else:
            out.append(
                {
                    "role": role,
                    "content": [{"type": text_type, "text": str(content)}],
                }
            )
    return out


def _extract_text(resp: dict[str, Any]) -> str:
    txt = resp.get("output_text")
    if isinstance(txt, str) and txt:
        return txt
    chunks: list[str] = []
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                t = c.get("text")
                if isinstance(t, str):
                    chunks.append(t)
    return "".join(chunks)


def _usage(resp: dict[str, Any]) -> dict[str, int]:
    u = resp.get("usage") or {}
    pt = u.get("input_tokens", u.get("prompt_tokens", 0)) or 0
    ct = u.get("output_tokens", u.get("completion_tokens", 0)) or 0
    tt = u.get("total_tokens", pt + ct) or (pt + ct)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt}


def build_app(upstream: str, timeout: float, expected_token: str | None) -> Starlette:
    client = httpx.AsyncClient(base_url=upstream.rstrip("/"), timeout=timeout)

    def _auth_ok(request: Request) -> bool:
        if not expected_token:
            return True
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], expected_token)
        return False

    async def models(request: Request) -> JSONResponse:
        if not _auth_ok(request):
            return JSONResponse(
                status_code=401, content={"error": {"message": "unauthorized"}}
            )
        try:
            r = await client.get("/v1/models")
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": {"message": f"upstream models failed: {e}"}},
            )

    async def chat_completions(request: Request) -> JSONResponse:
        if not _auth_ok(request):
            return JSONResponse(
                status_code=401, content={"error": {"message": "unauthorized"}}
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": {"message": "invalid json"}}
            )

        if body.get("stream"):
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "streaming not supported by shim"}},
            )

        model = body.get("model", "gpt-5.4")
        if "/" in model:
            model = model.split("/", 1)[1]
        messages = body.get("messages", [])
        payload: dict[str, Any] = {
            "model": model,
            "input": _messages_to_input(messages),
        }
        if body.get("max_tokens") is not None:
            payload["max_output_tokens"] = body["max_tokens"]
        if body.get("max_completion_tokens") is not None:
            payload["max_output_tokens"] = body["max_completion_tokens"]
        if body.get("response_format") is not None:
            payload["text"] = {"format": body["response_format"]}

        try:
            r = await client.post(
                "/v1/responses",
                json=payload,
                headers={"content-type": "application/json"},
            )
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": {"message": f"upstream request failed: {e}"}},
            )

        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = {"error": {"message": r.text}}
            return JSONResponse(status_code=r.status_code, content=err)

        try:
            resp = r.json()
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": {"message": f"upstream returned non-json: {e}"}},
            )

        text = _extract_text(resp)
        usage = _usage(resp)
        chat = {
            "id": resp.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": resp.get("created_at") or int(time.time()),
            "model": resp.get("model", model),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop"
                    if resp.get("status", "completed") == "completed"
                    else "length",
                }
            ],
            "usage": usage,
        }
        return JSONResponse(content=chat)

    routes = [
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--upstream", default="http://127.0.0.1:4141")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4152)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument(
        "--token",
        default=os.environ.get("COPILOT_SHIM_TOKEN"),
        help="Require Bearer token; if empty, auth is disabled.",
    )
    args = p.parse_args()
    app = build_app(args.upstream, args.timeout, args.token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
