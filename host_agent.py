#!/usr/bin/env python3
"""
Host-side helper for running the proxy in Docker.

Docker cannot read your browser profiles or macOS Keychain, so the admin page
cannot import a token by itself. This agent runs on the host, listens on
loopback only, and lets the admin page trigger the import:

    python host_agent.py                     # http://127.0.0.1:8001
    python host_agent.py --port 8001

The admin page (http://localhost:8000/) calls it directly from your browser.
It writes the token to ./session_data.json, which the container mounts, then the
page hot-reloads it. Nothing is exposed beyond localhost.
"""

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from browser_token import read_local_browser_token

TOKEN_FILE = os.path.abspath("./session_data.json")
ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        self.send_header("Access-Control-Allow-Origin", origin if origin in ALLOWED_ORIGINS else "null")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"detail": "Not found"})

    def do_POST(self):
        if self.path != "/import-token":
            self._json(404, {"detail": "Not found"})
            return
        try:
            token = read_local_browser_token()
        except Exception as e:
            self._json(400, {"detail": str(e)})
            return
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": token, "updated_at": int(time.time())}, f, indent=2)
        print(f"[HOST] Token imported to {TOKEN_FILE}")
        self._json(200, {"ok": True, "token_file": TOKEN_FILE})

    def log_message(self, fmt, *args):
        pass  # keep the console clean


def main():
    parser = argparse.ArgumentParser(description="Host helper: import the ChatGPT token for the Docker proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[HOST] Import agent on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
