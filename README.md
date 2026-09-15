# gpt2api — ChatGPT Web → OpenAI-compatible API

OpenAI-compatible proxy for ChatGPT Web. It talks to chatgpt.com's **private HTTP API directly**:
no browser, no Playwright, no Selenium. The sentinel handshake, proof-of-work and the Cloudflare
Turnstile challenge are all solved in pure Python.

- FastAPI server exposing `/v1/models` and `/v1/chat/completions` (stream + non-stream).
- Blocking HTTP via `curl_cffi` with a `firefox133` TLS fingerprint (required — Cloudflare blocks
  every `chrome*` fingerprint on chatgpt.com).
- Two modes: **anonymous** (no setup at all) and **authenticated** (uses your ChatGPT account).

---

## Requirements

- Docker (recommended), or Python 3.11+ / 3.13 with a venv.
- No browser needed. Nothing to log into unless you want authenticated mode.

---

## Quick start (Docker)

```bash
git clone <this repo> gpt2api
cd gpt2api
docker compose up -d --build
```

That's it. The proxy is now on `http://localhost:8000` in **anonymous** mode.

Verify:

```bash
curl -s http://localhost:8000/v1/models
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in exactly 3 words"}]}'
```

To use authenticated mode, see [Authenticated mode](#authenticated-mode).

---

## Quick start (without Docker)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py --port 8000
```

There is no test suite and no linter. The only check is a syntax pass:

```bash
venv/bin/python -m py_compile main.py chatgpt_client.py
```

---

## Modes

The active mode is printed in the logs for every request as `mode=auth` or `mode=anon`:

```bash
docker compose logs -f
```

### Anonymous mode (default)

Nothing to configure. Uses ChatGPT's `backend-anon` endpoints. Expect stricter rate limits and
occasional "you must log in" responses from upstream. Good for testing and light use.

### Authenticated mode

Uses ChatGPT's `backend-api` endpoints with your account's access token — higher limits and
access to your account's model defaults.

Pick **one** of these:

1. **Environment variable** (recommended for Docker):

   ```bash
   export CHATGPT_ACCESS_TOKEN="eyJhbGciOi..."
   docker compose up -d
   ```

   `docker-compose.yml` forwards the host's `CHATGPT_ACCESS_TOKEN` into the container.

2. **Session file** — `session_data.json` in the repo root:

   ```json
   { "access_token": "eyJhbGciOi...", "updated_at": 1789467497 }
   ```

   It is bind-mounted into the container by `docker-compose.yml`.

3. **CLI flag** (host runs only):

   ```bash
   venv/bin/python main.py --access-token "eyJhbGciOi..."
   ```

4. **Import from a logged-in browser** (macOS only — uses `browser_cookie3`):

   ```bash
   venv/bin/python main.py --import-token
   docker compose restart
   ```

   This scans your local Chrome/Arc profiles for a ChatGPT session, requests
   `https://chatgpt.com/api/auth/session`, and writes the token to `session_data.json`.
   If it reports *"No logged-in ChatGPT session found"*, log in to https://chatgpt.com in a
   normal browser first, then quit the browser (Chrome only flushes its cookie DB on exit).

> The token is read **once at startup**. After changing it, restart the proxy
> (`docker compose restart` or re-run `main.py`).
>
> An access token grants full access to your ChatGPT account. Treat it like a password. Never commit
> `session_data.json`.

---

## API

Base URL: `http://localhost:8000/v1` — `api_key` can be anything.

### `GET /v1/models`

Dynamic (fetched from the session's backend, cached 10 min; static fallback in anonymous mode):

```bash
curl -s http://localhost:8000/v1/models
```

### `POST /v1/chat/completions`

Non-streaming:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Explain quantum computing in one sentence."}],
    "stream": false
  }'
```

Streaming (SSE, OpenAI chunk format, ends with `data: [DONE]`):

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Count from 1 to 5."}],
    "stream": true
  }'
```

Multi-turn / system prompt (messages are flattened into a single prompt):

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "Answer in one word."},
      {"role": "user", "content": "Capital of France?"}
    ]
  }'
```

### Python `openai` SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")

stream = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Count from 1 to 5."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### Models

`GET /v1/models` is **dynamic**: it fetches the model list for the current session from
`GET /{backend-api|backend-anon}/models` and caches it for 10 minutes. Anonymous mode usually has no
such endpoint, so the proxy falls back to a small static list (`auto`, `gpt-4o`, `gpt-4o-mini`).

Any requested `model` is passed straight through as the slug, so valid slugs work even if they are
not in the list. Invalid or rate-limited slugs return a clear upstream error.

`model: "auto"` (the default) lets ChatGPT pick. The response's `model` field always reports the
**model actually used** (read from the backend's `model_slug`), not the one you asked for. It is also
logged:

```bash
docker compose logs -f | grep "Resolved model"
```

---

## Compatibility

Any OpenAI-compatible client works with:

- **Base URL**: `http://localhost:8000/v1`
- **API key**: anything (ignored — e.g. `dummy`, `not-needed`)
- **Model**: `auto`, or any slug from `GET /v1/models`

If the client runs on another machine or in another container, replace `localhost` with this
machine's LAN IP (and make sure port 8000 is reachable).

### What is *not* supported

This proxy is a thin chat bridge. It does **not** implement:

- ❌ **tool / function calling** — `tools`, `tool_choice`, `tool_calls` are ignored.
- ❌ **audio** (TTS/STT, voice mode).
- ❌ **`response_format` / JSON mode**, **logprobs**, `n`, `temperature`, `top_p` (ignored).
- ❌ **server-side history** — every request is a new conversation. Send the full message list
  each time; the proxy flattens it into one prompt.

**Supported:** text chat, **images (vision)**, and **file attachments** (PDF, text, code, office
docs) — the latter two in authenticated mode only.

Agentic tools that depend on tool calling will not work well. This proxy is best for chat-style
completions, scripts, and simple assistants.

### Images and file attachments

Send OpenAI-style `content` parts. Both remote URLs and `data:` URIs work, plus local file paths.

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "What word is in this image?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/pic.png"}}
      ]
    }]
  }'
```

Base64 (no hosting needed):

```json
{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo..."}}
```

Files use the `file` part with `file_data`:

```json
{"type": "file", "file": {"file_data": "data:application/pdf;base64,JVBERi0xLjQ..."}}
```

How it works: attachments are uploaded to ChatGPT (`POST /backend-api/files` → blob `PUT` →
confirm), then referenced from the user message as `image_asset_pointer` parts and
`metadata.attachments`. Images and documents **require authenticated mode** (the anon API has no
file endpoints) — without a token the request fails with a clear error.

Python:

```python
resp = client.chat.completions.create(
    model="auto",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ],
    }],
)
```


---

## How it works

Per request, `chatgpt_client.py` does:

1. `GET https://chatgpt.com/` — bootstrap to discover script sources / build id.
2. `POST /{backend-api|backend-anon}/sentinel/chat-requirements/prepare` with a generated
   requirements token (`p`) → `prepare_token`, an optional proof-of-work challenge, and an optional
   Turnstile challenge (`dx`).
3. Solve in Python:
   - **proof-of-work** — hash until the difficulty prefix matches
     (auth: SHA3-512; anon: FNV-1a + murmur3 fmix32).
   - **Turnstile** — `dx` is base64 → XOR with the `p` token → a JSON opcode program, executed by
     the interpreter in `solve_turnstile()`.
4. `POST .../sentinel/chat-requirements/finalize` → sentinel token.
5. `POST .../f/conversation/prepare` → `conduit_token`.
6. `POST .../f/conversation` (SSE) with the sentinel + conduit headers → streamed answer.

Sentinel and conduit tokens are short-lived, so a fresh pair is fetched for every request.

### Files

| File | Purpose |
| --- | --- |
| `main.py` | FastAPI layer + CLI. No protocol logic. |
| `chatgpt_client.py` | All protocol code (sentinel, PoW, Turnstile, conduit, SSE). |
| `session_data.json` | Optional access token. Must be a **file** (see Gotchas). |
| `Dockerfile`, `docker-compose.yml` | Container build/run. No browser installed. |
| `requirements.txt` | Runtime dependencies. |

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `mode=anon` in logs when you set a token | The token was not found. Check `CHATGPT_ACCESS_TOKEN` is exported in the shell that runs `docker compose`, or that `session_data.json` is a valid JSON **file**. |
| Auth suddenly fails / upstream 401-403 | Access token expired. Re-run `--import-token`, or paste a fresh one, then restart. |
| `session_data.json` ignored silently | It is a **directory**, not a file. Docker bind-mounts it as a directory and token loading falls through to anonymous. `rm -rf session_data.json && printf '{}' > session_data.json`. |
| Upstream 403 on every request | Usually a stale/rejected client build. Update the client constants in `chatgpt_client.py` (`*_CLIENT_VERSION`, `*_CLIENT_BUILD`) and retry. |
| `sentinel requires arkose token` | Upstream asked for an Arkose challenge; this client does not implement it. Retry later / use authenticated mode. |
| Request hangs for a long time | Proof-of-work is CPU-bound (~up to 500k hashes). Expected occasionally. |
| Turnstile solver returns`None` | The `dx` payload changed upstream. Inspect `solve_turnstile()` against a fresh capture. |

Do not "fix" the following by removing them — they are load-bearing:

- `impersonate="firefox133"` — Cloudflare rejects `chrome*` fingerprints.
- The conduit flow (`f/conversation/prepare` → `f/conversation`) — the legacy
  `/backend-api/conversation` returns 403.
- Auth vs anon differences: different base path, different finalize field names
  (`proof_token`/`turnstile_token` vs `proofofwork`/`turnstile`), and different PoW algorithms.
- Keeping the blocking client off the event loop (`asyncio.to_thread` / `run_in_executor`).

---

## Limitations

- One upstream session; requests are independent (no conversation history is persisted server-side).
- Anonymous mode is rate-limited by ChatGPT and intended for testing.
- `--import-token` is macOS-only; on Linux/Windows set the token manually or via env.
- The client constants and solvers are tightly coupled to the current ChatGPT web build and may need
  updating when upstream changes.
