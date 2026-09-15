# AGENTS.md

OpenAI-compatible proxy that talks to chatgpt.com's **private HTTP API directly**.
No browser, no Playwright. Sentinel handshake, proof-of-work and Cloudflare
Turnstile are solved in pure Python.

## Layout

- `main.py` — FastAPI layer only (`/v1/models`, `/v1/chat/completions`, CLI). No protocol logic.
- `chatgpt_client.py` — all protocol code: bootstrap, sentinel prepare/finalize, PoW,
  Turnstile bytecode solver, `f/conversation/prepare` (conduit), SSE streaming.
  Public entrypoint: `ChatGPTClient.stream(prompt, model) -> Iterator[str]` (blocking, `curl_cffi`).
- `session_data.json` — optional `{"access_token": "...", "updated_at": ...}`. Bind-mounted into the container.
- `README.md` covers usage/scenarios and matches the code; keep it in sync when behaviour changes.

## Models

- `GET /v1/models` is dynamic: `ChatGPTClient.list_models()` fetches `GET /{base}/models`
  (auth) with a 10-min in-process cache; falls back to `FALLBACK_MODELS` (auto/gpt-4o/gpt-4o-mini)
  when unavailable (anonymous mode).
- The `model` field in responses is the backend-resolved slug, parsed from `metadata.model_slug`
  in SSE events (`_extract_slug`). Note: the user-message echo has `default_model_slug`, which is
  NOT the resolved model — do not use it. `auto` currently resolves to `gpt-5-6-mini`.
- Upstream error events (`{"error": ..., "error_code": ...}`) are raised as `RuntimeError` and
  surfaced as HTTP 502 / an SSE error chunk. Free-plan limits show up as `usage_limit`.

## Behaviour caveats

- Requests are **stateless**: `stream()` sends one user message and starts a fresh conversation each
  call. Multi-turn context only exists if the caller resends the whole message list (flattened by
  `format_prompt`).
- Only plain text plus **images and file attachments** are supported. `tools`/`function calling`,
  audio, `response_format`/JSON mode, `temperature`, `top_p`, and `n` are **not** implemented — do
  not claim otherwise in docs or tests. Agentic clients that need tool calling will not work well.

## Attachments (images / files)

- `ChatGPTClient.stream(..., attachments=[url])` accepts http(s) URLs, `data:` URIs, or local paths.
- Flow: `POST /{base}/files` (slot) -> blob `PUT` (`x-ms-blob-type: BlockBlob`, **auth headers must
  be removed**) -> `POST /files/{id}/uploaded` -> for non-images poll `GET /files/{id}` until
  `retrieval_index_status == "success"`.
- Message shape: `content_type: multimodal_text`; images become
  `{"content_type": "image_asset_pointer", "asset_pointer": "file-service://<id>", ...}` parts;
  documents only appear in `metadata.attachments`.
- **Authenticated mode only** — `backend-anon` has no file endpoints; `_user_message` raises.
- `Pillow` is used only to read image dimensions for the pointer parts.

## Commands

```bash
# host deps (venv is python3.13; container is python3.11)
venv/bin/pip install -r requirements.txt

# run
docker compose up -d --build          # container, mounts ./session_data.json
venv/bin/python main.py --port 8000   # host
venv/bin/python main.py --import-token  # read access token from local browser (macOS only)

# syntax check (no test suite, no linter configured)
venv/bin/python -m py_compile main.py chatgpt_client.py
```

Verify: `curl -s localhost:8000/v1/models`, then a `/v1/chat/completions` request.
`docker compose logs` prints `mode=auth` or `mode=anon` per request — use it to confirm mode.

## Modes

- **Anonymous (default)**: no config. Uses `backend-anon`.
- **Authenticated**: `CHATGPT_ACCESS_TOKEN` env, or `session_data.json`, or `--access-token`.
  Uses `backend-api`. Token is read once at startup by `get_client()` — restart to pick up a new one.

## Hard-won protocol constraints (do not "simplify" away)

- TLS impersonation **must be `firefox133`**. Cloudflare blocks all `chrome*` fingerprints on chatgpt.com.
- Conversation **must** use the conduit flow: `f/conversation/prepare` -> `conduit_token` -> `f/conversation`.
  The legacy `/backend-api/conversation` returns 403.
- Auth vs anon differ in more than the path, keep both paths in sync:
  - finalize body fields: auth `proof_token`/`turnstile_token` vs anon `proofofwork`/`turnstile`.
  - PoW algorithm: auth = sha3-512; anon = FNV-1a + murmur3 fmix32.
- Turnstile `dx` is XOR-encrypted with the requirements token (`p`), then executed by the
  opcode interpreter in `solve_turnstile()`. It is not a stubbed/optional value.
- Sentinel tokens are short-lived; a fresh sentinel + conduit token is fetched per request by design.
- `ChatGPTClient` is sync/blocking. Keep it off the event loop (`asyncio.to_thread` /
  `run_in_executor` as done in `main.py`).

## Gotchas

- `session_data.json` must be a **file**. If it is a directory, the bind mount shadows it as a
  directory and token loading silently falls through to anonymous mode.
- `--import-token` uses `browser_cookie3` and is macOS-specific (Chrome/Arc profiles).
- `browser_data/` is a leftover from the removed browser approach; safe to delete.
