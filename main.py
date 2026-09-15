#!/usr/bin/env python3
"""
OpenAI-compatible proxy for ChatGPT Web.

No browser required: the sentinel handshake, proof-of-work and the Cloudflare
Turnstile challenge are all solved in Python (see chatgpt_client.py).

Runs anonymously by default. For an authenticated session (higher limits,
account models) provide a ChatGPT access token:

    CHATGPT_ACCESS_TOKEN=<token> python main.py
    # or grab one from a logged-in browser:
    python main.py --import-token
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from admin_ui import PAGE
from browser_token import read_local_browser_token
from chatgpt_client import ChatGPTClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("chatgpt-web-proxy")

TOKEN_FILE = os.path.abspath("./session_data.json")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
# Browser cookie reading needs the host's browser profiles + Keychain, so it only works when this
# process runs directly on the machine (not inside the container).
IN_CONTAINER = os.path.exists("/.dockerenv")
CAN_IMPORT = not IN_CONTAINER

app = FastAPI(title="ChatGPT Web OpenAI-Compatible Proxy")

_client: ChatGPTClient = None


class ChatMessage(BaseModel):
    role: str
    content: object

    def get_text_content(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts = []
            for item in self.content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(parts)
        return str(self.content)

    def get_attachments(self) -> list:
        """Image/file URLs from OpenAI-style `content` parts."""
        urls = []
        if isinstance(self.content, list):
            for item in self.content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image_url":
                    image = item.get("image_url")
                    if isinstance(image, str):
                        urls.append(image)
                    elif isinstance(image, dict) and image.get("url"):
                        urls.append(image["url"])
                elif item.get("type") == "file" and isinstance(item.get("file"), dict):
                    if item["file"].get("file_data"):
                        urls.append(item["file"]["file_data"])
        return urls


class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: List[ChatMessage]
    stream: bool = False


def load_access_token() -> str:
    token = os.environ.get("CHATGPT_ACCESS_TOKEN", "").strip()
    if token:
        return token
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                return (json.load(f).get("access_token") or "").strip()
        except Exception as e:
            logger.warning(f"Could not read {TOKEN_FILE}: {e}")
    return ""


def get_client() -> ChatGPTClient:
    global _client
    if _client is None:
        token = load_access_token()
        _client = ChatGPTClient(access_token=token)
        logger.info(f"ChatGPT client ready (mode={'authenticated' if token else 'anonymous'}).")
    return _client


def format_prompt(messages: List[ChatMessage]) -> str:
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].get_text_content()
    parts = [f"[{m.role.capitalize()}]:\n{m.get_text_content()}" for m in messages]
    parts.append("[Assistant]:\n")
    return "\n\n".join(parts)


def save_access_token(token: str, allow_empty: bool = False) -> None:
    """Persist the token. Empty values are ignored unless explicitly clearing."""
    if not token and not allow_empty:
        logger.warning("Refusing to overwrite the stored token with an empty value.")
        return
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": token, "updated_at": int(time.time())}, f, indent=2)


def set_client(access_token: str) -> ChatGPTClient:
    """Swap the active client (hot reload — no restart needed)."""
    global _client
    _client = ChatGPTClient(access_token=access_token)
    logger.info(f"ChatGPT client reloaded (mode={'authenticated' if access_token else 'anonymous'}).")
    return _client


# --- admin UI ---------------------------------------------------------------

def import_token() -> int:
    """CLI: read a token from a local browser and persist it."""
    try:
        token = read_local_browser_token()
    except Exception as e:
        print(f"[TOKEN] {e}")
        return 1
    save_access_token(token, allow_empty=False)
    print(f"[TOKEN] Saved to {TOKEN_FILE}. Start the proxy with: python main.py")
    return 0


def _check_admin(request: Request) -> None:
    if not ADMIN_KEY:
        return
    key = request.headers.get("x-admin-key") or request.query_params.get("key", "")
    if key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key.")


class TokenRequest(BaseModel):
    access_token: str = ""


class TestRequest(BaseModel):
    prompt: str = "Reply with exactly: ok"


@app.get("/", response_class=HTMLResponse)
async def admin_page(request: Request):
    _check_admin(request)
    return HTMLResponse(PAGE)


@app.get("/admin/status")
async def admin_status(request: Request):
    _check_admin(request)
    client = get_client()
    account = None
    model_count = 0
    if client.authenticated:
        account = await asyncio.to_thread(client.whoami)
        try:
            model_count = len(await asyncio.to_thread(client.list_models))
        except Exception:
            model_count = 0
    return {
        "authenticated": client.authenticated,
        "account": account,
        "model_count": model_count,
        "token_file": os.path.exists(TOKEN_FILE),
        "admin_key_required": bool(ADMIN_KEY),
        "can_import": CAN_IMPORT,
    }


@app.post("/admin/reload")
async def admin_reload(request: Request):
    """Re-read session_data.json into the running client (no restart)."""
    _check_admin(request)
    token = load_access_token()
    set_client(token)
    return {"ok": True, "authenticated": get_client().authenticated}


@app.post("/admin/token")
async def admin_set_token(payload: TokenRequest, request: Request):
    _check_admin(request)
    token = payload.access_token.strip()
    previous = get_client().access_token
    if token:
        client = set_client(token)
        if not await asyncio.to_thread(client.valid_token):
            set_client(previous)  # keep the working session
            raise HTTPException(status_code=400, detail="Token rejected by ChatGPT. Check it and try again.")
        save_access_token(token)
    else:
        set_client("")
        save_access_token("", allow_empty=True)
    return {"ok": True, "authenticated": get_client().authenticated}


@app.post("/admin/import")
async def admin_import(request: Request):
    _check_admin(request)
    if not CAN_IMPORT:
        raise HTTPException(
            status_code=400,
            detail=(
                "Browser import is unavailable inside Docker (it cannot read your machine's browser "
                "profiles or Keychain). Run 'venv/bin/python main.py --import-token' on the host, "
                "then click Reload."
            ),
        )
    try:
        token = await asyncio.to_thread(read_local_browser_token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    set_client(token)
    save_access_token(token)
    return {"ok": True, "authenticated": True}


@app.post("/admin/models/refresh")
async def admin_refresh_models(request: Request):
    _check_admin(request)
    client = get_client()
    client._models_cache = None
    client._models_ts = 0
    models = await asyncio.to_thread(client.list_models)
    return {"ok": True, "count": len(models), "models": [m["id"] for m in models]}


@app.post("/admin/test")
async def admin_test(payload: TestRequest, request: Request):
    _check_admin(request)
    resolved: dict = {}
    try:
        text = await asyncio.to_thread(
            lambda: "".join(get_client().stream(payload.prompt, model="auto", resolved=resolved))
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "model": resolved.get("slug") or "auto", "content": text}


@app.get("/v1/models")
async def list_models():
    try:
        models = await asyncio.to_thread(get_client().list_models)
    except Exception as e:
        logger.warning(f"Falling back to static model list: {e}")
        models = [
            {"id": slug, "object": "model", "created": 1715000000, "owned_by": "openai"}
            for slug in ("auto", "gpt-4o", "gpt-4o-mini")
        ]
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if not req.messages:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "Field 'messages' cannot be empty.", "type": "invalid_request_error", "code": "messages_empty"}},
        )

    prompt = format_prompt(req.messages)
    model = req.model or "auto"
    req_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    attachments: list = []
    for message in reversed(req.messages):
        if message.role == "user":
            attachments = message.get_attachments()
            break

    logger.info(f"Completion {req_id} model={model} stream={req.stream} attachments={len(attachments)} mode={'auth' if get_client().authenticated else 'anon'}")

    if req.stream:
        async def generator():
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            resolved: dict = {}

            def produce():
                try:
                    for delta in get_client().stream(prompt, model=model, resolved=resolved, attachments=attachments):
                        loop.call_soon_threadsafe(queue.put_nowait, ("delta", delta))
                    loop.call_soon_threadsafe(queue.put_nowait, ("end", None))
                except Exception as e:  # surface upstream errors
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

            loop.run_in_executor(None, produce)

            while True:
                kind, value = await queue.get()
                if kind == "delta":
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": resolved.get("slug") or model,
                        "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                elif kind == "error":
                    yield f"data: {json.dumps({'error': {'message': value, 'type': 'server_error'}})}\n\n"
                    break
                else:
                    break

            final = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": resolved.get("slug") or model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream")

    resolved: dict = {}

    def collect() -> str:
        return "".join(get_client().stream(prompt, model=model, resolved=resolved, attachments=attachments))

    try:
        text = await asyncio.to_thread(collect)
    except Exception as e:
        logger.error(f"Completion failed: {e}")
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": str(e), "type": "server_error", "code": "upstream_error"}},
        )

    if resolved.get("slug"):
        logger.info(f"Resolved model: {resolved['slug']}")

    return {
        "id": req_id,
        "object": "chat.completion",
        "created": created,
        "model": resolved.get("slug") or model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(text) // 4,
            "total_tokens": (len(prompt) + len(text)) // 4,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="OpenAI-compatible proxy for ChatGPT Web (no browser required)")
    parser.add_argument("--import-token", action="store_true", help="Import a ChatGPT access token from a local browser (one-time)")
    parser.add_argument("--access-token", default="", help="ChatGPT access token (overrides env/file)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.import_token:
        sys.exit(import_token())

    if args.access_token:
        os.environ["CHATGPT_ACCESS_TOKEN"] = args.access_token

    get_client()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
