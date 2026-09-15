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
from typing import List

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from chatgpt_client import ChatGPTClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("chatgpt-web-proxy")

TOKEN_FILE = os.path.abspath("./session_data.json")

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


def import_token() -> int:
    """Read a ChatGPT access token from a locally logged-in browser (one-time)."""
    try:
        import browser_cookie3
        import urllib.request
    except ImportError:
        print("[TOKEN] browser_cookie3 not installed. Run: pip install browser_cookie3")
        return 1

    import urllib.request

    cookie_header = ""
    for reader in (browser_cookie3.arc, browser_cookie3.chrome):
        try:
            jar = list(reader(domain_name="chatgpt.com"))
        except Exception:
            continue
        if any(c.name.startswith("__Secure-next-auth.session-token") for c in jar):
            cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)
            break

    if not cookie_header:
        print("[TOKEN] No logged-in ChatGPT session found in a local browser.")
        return 1

    req = urllib.request.Request(
        "https://chatgpt.com/api/auth/session",
        headers={"Cookie": cookie_header, "User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    token = (data.get("accessToken") or "").strip()
    if not token:
        print("[TOKEN] Session found but no access token (expired login?).")
        return 1
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": token, "updated_at": int(time.time())}, f, indent=2)
    print(f"[TOKEN] Saved to {TOKEN_FILE}. Start the proxy with: python main.py")
    return 0


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
