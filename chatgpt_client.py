"""
ChatGPT web protocol client — no browser required.

Pure-Python implementation of the chatgpt.com private API:
  1. Bootstrap the site to discover script sources / build id.
  2. POST sentinel/chat-requirements/prepare  -> prepare_token + PoW + turnstile challenge
  3. Solve proof-of-work and the Cloudflare Turnstile challenge in Python.
  4. POST sentinel/chat-requirements/finalize -> sentinel token
  5. POST f/conversation/prepare              -> conduit token
  6. POST f/conversation (SSE)                -> streamed answer

Works authenticated (backend-api, needs an access token) or anonymously
(backend-anon). Algorithms mirror the real client; TLS fingerprint must be
firefox133 (Cloudflare rejects chrome* fingerprints).
"""

import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Iterator, List, Optional, Sequence

import curl_cffi.requests as requests

logger = logging.getLogger("chatgpt-client")

BASE_URL = "https://chatgpt.com"

AUTH_CLIENT_VERSION = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"
AUTH_CLIENT_BUILD = "6708908"
ANON_CLIENT_VERSION = "prod-5e453451adb2de3afe642039d5230eb40e1f57b9"
ANON_CLIENT_BUILD = "7436895"

AUTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)
ANON_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)
SENTINEL_SDK_FALLBACK = f"{BASE_URL}/backend-api/sentinel/sdk.js"

# Used when the upstream model list cannot be fetched (e.g. anonymous mode).
FALLBACK_MODELS = ["auto", "gpt-4o", "gpt-4o-mini"]

# Attachment handling (mirrors the web client's file upload flow).
MULTIMODAL_MIME_TYPES = ["image/jpeg", "image/webp", "image/png", "image/gif"]
MY_FILES_MIME_TYPES = [
    "text/plain", "text/markdown", "text/html", "text/css", "text/xml", "text/csv",
    "application/json", "application/xml", "application/pdf", "application/zip",
    "application/msword", "application/rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/javascript", "text/x-python", "text/x-script.python", "text/x-java",
    "text/x-c", "text/x-c++", "text/x-csharp", "text/x-php", "text/x-ruby",
    "text/x-typescript", "text/x-sh", "text/x-tex", "application/x-latex",
]
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "application/pdf": ".pdf", "application/json": ".json", "application/zip": ".zip",
    "text/plain": ".txt", "text/markdown": ".md", "text/html": ".html", "text/csv": ".csv",
}
DEFAULT_MIME = "application/octet-stream"


def new_uuid() -> str:
    return str(uuid.uuid4())


def _utf8_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def determine_use_case(mime_type: str) -> str:
    if mime_type in MULTIMODAL_MIME_TYPES:
        return "multimodal"
    if mime_type in MY_FILES_MIME_TYPES:
        return "my_files"
    return "ace_upload"


def file_extension(mime_type: str) -> str:
    return MIME_EXTENSIONS.get(mime_type, "")


def image_size(content: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        with Image.open(io.BytesIO(content)) as img:
            return img.width, img.height
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# HTML bootstrap
# ---------------------------------------------------------------------------

class _ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: List[str] = []
        self.data_build = ""

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        src = dict(attrs).get("src")
        if not src:
            return
        self.sources.append(src)
        match = re.search(r"c/[^/]*/_", src)
        if match:
            self.data_build = match.group(0)


def parse_resources(html: str) -> tuple[List[str], str]:
    parser = _ScriptSrcParser()
    parser.feed(html)
    sources = parser.sources or [SENTINEL_SDK_FALLBACK]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html)
        if match:
            data_build = match.group(1)
    return sources, data_build


# ---------------------------------------------------------------------------
# Proof of work — authenticated flavour (sha3-512)
# ---------------------------------------------------------------------------

def _legacy_parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


_NAVIGATOR_KEYS = [
    "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
    "storage−[object StorageManager]",
    "locks−[object LockManager]",
    "appCodeName−Mozilla",
    "permissions−[object Permissions]",
    "share−function share() { [native code] }",
    "webdriver−false",
    "managed−[object NavigatorManagedData]",
    "canShare−function canShare() { [native code] }",
    "vendor−Google Inc.",
    "mediaDevices−[object MediaDevices]",
    "vibrate−function vibrate() { [native code] }",
    "storageBuckets−[object StorageBucketManager]",
    "mediaCapabilities−[object MediaCapabilities]",
    "cookieEnabled−true",
    "virtualKeyboard−[object VirtualKeyboard]",
    "product−Gecko",
    "presentation−[object Presentation]",
    "onLine−true",
    "mimeTypes−[object MimeTypeArray]",
    "credentials−[object CredentialsContainer]",
    "serviceWorker−[object ServiceWorkerContainer]",
    "keyboard−[object Keyboard]",
    "gpu−[object GPU]",
    "doNotTrack",
    "serial−[object Serial]",
    "pdfViewerEnabled−true",
    "language−en-US",
    "geolocation−[object Geolocation]",
    "userAgentData−[object NavigatorUAData]",
    "getUserMedia−function getUserMedia() { [native code] }",
    "sendBeacon−function sendBeacon() { [native code] }",
    "hardwareConcurrency−32",
    "windowControlsOverlay−[object WindowControlsOverlay]",
]

_WINDOW_KEYS = [
    "0", "window", "self", "document", "name", "location",
    "customElements", "history", "navigation", "innerWidth", "innerHeight",
    "scrollX", "scrollY", "visualViewport", "screenX", "screenY",
    "outerWidth", "outerHeight", "devicePixelRatio", "screen", "chrome",
    "navigator", "onresize", "performance", "crypto", "indexedDB",
    "sessionStorage", "localStorage", "scheduler", "alert", "atob", "btoa",
    "fetch", "matchMedia", "postMessage", "queueMicrotask",
    "requestAnimationFrame", "setInterval", "setTimeout", "caches",
    "__NEXT_DATA__", "__BUILD_MANIFEST", "__NEXT_PRELOADREADY",
]

_DOCUMENT_KEYS = ["__reactContainer$fzelfjyxej8", "_reactListening5dehydibo78", "location"]
_SCREENS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160], [1680, 1050]]
_CORES = [8, 10, 12, 16]


def build_pow_config(user_agent: str, sources: Sequence[str], data_build: str) -> List[Any]:
    screen = random.choice(_SCREENS)
    return [
        sum(screen),
        _legacy_parse_time(),
        4294705152,
        1,
        user_agent,
        random.choice(list(sources)) if sources else SENTINEL_SDK_FALLBACK,
        data_build,
        "en-US",
        "en-US,en",
        random.random(),
        random.choice(_NAVIGATOR_KEYS),
        random.choice(_DOCUMENT_KEYS),
        random.choice(_WINDOW_KEYS),
        time.perf_counter() * 1000,
        new_uuid(),
        "",
        random.choice(_CORES),
        time.time() * 1000 - (time.perf_counter() * 1000),
        0, 0, 0, 0, 0, 0, 0,
    ]


def _pow_generate(seed: str, difficulty: str, config: List[Any], limit: int = 500000) -> tuple[str, bool]:
    target = bytes.fromhex(difficulty)
    diff_len = len(difficulty) // 2
    seed_bytes = seed.encode()
    static_1 = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode()
    static_2 = ("," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ",").encode()
    static_3 = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode()
    for i in range(limit):
        final_json = static_1 + str(i).encode() + static_2 + str(i >> 1).encode() + static_3
        encoded = base64.b64encode(final_json)
        digest = hashlib.sha3_512(seed_bytes + encoded).digest()
        if digest[:diff_len] <= target:
            return encoded.decode(), True
    return base64.b64encode(f'"{seed}"'.encode()).decode(), False


def build_requirements_token(config: List[Any]) -> str:
    return "gAAAAAC" + _utf8_b64(json.dumps(config, separators=(",", ":"), ensure_ascii=False))


def build_proof_token(seed: str, difficulty: str, config: List[Any]) -> str:
    answer, _ = _pow_generate(seed, difficulty, config)
    return "gAAAAAB" + answer


# ---------------------------------------------------------------------------
# Proof of work — anonymous flavour (FNV-1a + murmur3 fmix32)
# ---------------------------------------------------------------------------

def _fnv1a_fmix32(text: str) -> str:
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= h >> 16
    return format(h & 0xFFFFFFFF, "08x")


def _anon_pow_config(user_agent: str, script_source: Optional[str], client_version: str) -> List[Any]:
    screen = random.choice(_SCREENS)
    return [
        sum(screen),
        _legacy_parse_time(),
        4395630592,
        1,
        user_agent,
        script_source or SENTINEL_SDK_FALLBACK,
        client_version,
        "en-US",
        "en-US,en",
        0,
        random.choice(_NAVIGATOR_KEYS),
        random.choice(_DOCUMENT_KEYS),
        random.choice(_WINDOW_KEYS),
        time.perf_counter() * 1000,
        new_uuid(),
        "",
        random.choice(_CORES),
        time.time() * 1000 - time.perf_counter() * 1000,
        0, 0, 0, 0, 0, 0, 0,
    ]


def _anon_solve_pow(seed: str, difficulty: str, config: List[Any], limit: int = 500000) -> Optional[str]:
    if not difficulty:
        return None
    diff_len = len(difficulty)
    cfg = list(config)
    start = time.perf_counter()
    for nonce in range(limit):
        cfg[3] = nonce
        cfg[9] = round((time.perf_counter() - start) * 1000)
        serialized = _utf8_b64(json.dumps(cfg, separators=(",", ":"), ensure_ascii=False))
        if _fnv1a_fmix32(seed + serialized)[:diff_len] <= difficulty:
            return serialized + "~S"
    return None


def anon_requirements_token(config: List[Any]) -> str:
    return "gAAAAAC" + _utf8_b64(json.dumps(config, separators=(",", ":"), ensure_ascii=False))


def anon_proof_token(seed: str, difficulty: str, config: List[Any]) -> str:
    answer = _anon_solve_pow(seed, difficulty, config)
    if answer is None:
        return "gAAAAAB" + _utf8_b64("e")
    return "gAAAAAB" + answer


# ---------------------------------------------------------------------------
# Turnstile solver (dx bytecode interpreter)
# ---------------------------------------------------------------------------

class _OrderedMap:
    def __init__(self) -> None:
        self.keys: List[str] = []
        self.values: Dict[str, Any] = {}

    def add(self, key: str, value: Any) -> None:
        if key not in self.values:
            self.keys.append(key)
        self.values[key] = value


_TURNSTILE_SPECIAL = {
    "window.Math": "[object Math]",
    "window.Reflect": "[object Reflect]",
    "window.performance": "[object Performance]",
    "window.localStorage": "[object Storage]",
    "window.Object": "function Object() { [native code] }",
    "window.Reflect.set": "function set() { [native code] }",
    "window.performance.now": "function () { [native code] }",
    "window.Object.create": "function create() { [native code] }",
    "window.Object.keys": "function keys() { [native code] }",
    "window.Math.random": "function random() { [native code] }",
}

_LOCALSTORAGE_KEYS = [
    "STATSIG_LOCAL_STORAGE_INTERNAL_STORE_V4",
    "STATSIG_LOCAL_STORAGE_STABLE_ID",
    "client-correlated-secret",
    "oai/apps/capExpiresAt",
    "oai-did",
    "STATSIG_LOCAL_STORAGE_LOGGING_REQUEST",
    "UiState.isNavigationCollapsed.1",
]


def _to_str(value: Any) -> str:
    if value is None:
        return "undefined"
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return _TURNSTILE_SPECIAL.get(value, value)
    if isinstance(value, list) and all(isinstance(i, str) for i in value):
        return ",".join(value)
    return str(value)


def _to_num(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _js_add(a: Any, b: Any) -> Any:
    if isinstance(a, bool) or isinstance(b, bool):
        return _to_str(a) + _to_str(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + b
    return _to_str(a) + _to_str(b)


def _xor_string(text: str, key: str) -> str:
    if not key:
        return text
    return "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(text))


def solve_turnstile(dx: str, key: str, script_sources: Optional[Sequence[str]] = None) -> Optional[str]:
    """Run the obfuscated Turnstile bytecode contained in `dx`, XOR-keyed by the requirements token."""
    try:
        token_list = json.loads(_xor_string(base64.b64decode(dx).decode(), key))
    except Exception:
        return None

    pm: Dict[Any, Any] = {}
    start_time = time.time()
    box = {"result": None, "done": False}

    def prop(obj: Any, key: Any) -> Any:
        if obj is None:
            return None
        ks = _to_str(key)
        if isinstance(obj, str):
            joined = f"{obj}.{ks}"
            return f"{BASE_URL}/" if joined == "window.document.location" else joined
        if isinstance(obj, _OrderedMap):
            return obj.values.get(ks)
        if isinstance(obj, dict):
            return obj.get(ks)
        if isinstance(obj, list):
            try:
                return obj[int(key)]
            except (ValueError, IndexError, TypeError):
                return None
        return f"{_to_str(obj)}.{ks}"

    def call(target: Any, args: List[Any]) -> Any:
        if isinstance(target, str):
            if target == "window.Math.random":
                return random.random()
            if target == "window.performance.now":
                return (time.time_ns() - int(start_time * 1e9) + random.random()) / 1e6
            if target == "window.Object.create":
                return _OrderedMap()
            if target == "window.Object.keys":
                arg = args[0] if args else None
                if _to_str(arg) == "window.localStorage":
                    return list(_LOCALSTORAGE_KEYS)
                if isinstance(arg, _OrderedMap):
                    return list(arg.keys)
                if isinstance(arg, dict):
                    return list(arg.keys())
                if isinstance(arg, list):
                    return [str(i) for i in range(len(arg))]
                return []
            if target == "window.Reflect.set":
                obj, k, v = (args + [None, None, None])[:3]
                if isinstance(obj, _OrderedMap):
                    obj.add(_to_str(k), v)
                return None
            if target == "window.btoa":
                return base64.b64encode(_to_str(args[0] if args else "").encode()).decode()
            if target == "window.atob":
                try:
                    return base64.b64decode(_to_str(args[0] if args else "")).decode()
                except Exception:
                    return ""
            if target == "window.JSON.stringify":
                try:
                    return json.dumps(args[0] if args else None)
                except Exception:
                    return "null"
            if target == "window.JSON.parse":
                try:
                    return json.loads(_to_str(args[0] if args else "null"))
                except Exception:
                    return None
            return None
        if callable(target):
            try:
                return target(*args)
            except Exception:
                return None
        return None

    def op0(*args):  # recursive sub-queue
        sub = args[0] if args else None
        if isinstance(sub, str) and sub:
            saved = pm.get(9)
            try:
                pm[9] = json.loads(_xor_string(base64.b64decode(sub).decode(), key))
                run()
            except Exception:
                pass
            finally:
                pm[9] = saved

    def op1(n, e):
        pm[n] = _xor_string(_to_str(pm.get(n)), _to_str(pm.get(e)))

    def op2(t, n):
        pm[t] = n

    def op3(t):
        if not box["done"]:
            box["done"] = True
            box["result"] = base64.b64encode(_to_str(t).encode()).decode()

    def op4(t):
        if not box["done"]:
            box["done"] = True
            box["result"] = base64.b64encode(_to_str(t).encode()).decode()

    def op5(n, e):
        cur = pm.get(n)
        if isinstance(cur, list):
            cur.append(pm.get(e))
        else:
            pm[n] = _js_add(cur, pm.get(e))

    def op6(n, e, r):
        pm[n] = prop(pm.get(e), pm.get(r))

    def op7(n, *e):
        call(pm.get(n), [pm.get(x) for x in e])

    def op8(n, e):
        pm[n] = pm.get(e)

    def op11(n, e):
        pattern = _to_str(pm.get(e))
        rx = None
        try:
            rx = re.compile(pattern)
        except re.error:
            pass
        found = None
        for src in (script_sources or []):
            if rx and rx.search(src):
                found = src
                break
        pm[n] = found

    def op12(n):
        pm[n] = pm

    def op13(n, e, *r):
        try:
            call(pm.get(e), list(r))
        except Exception:
            pm[n] = "error"

    def op14(n, e):
        pm[n] = json.loads(_to_str(pm.get(e)))

    def op15(n, e):
        v = pm.get(e)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        pm[n] = json.dumps(v)

    def op17(n, e, *r):
        try:
            pm[n] = call(pm.get(e), [pm.get(x) for x in r])
        except Exception as ex:
            pm[n] = str(ex)

    def op18(n):
        pm[n] = base64.b64decode(_to_str(pm.get(n))).decode()

    def op19(n):
        pm[n] = base64.b64encode(_to_str(pm.get(n)).encode()).decode()

    def op20(n, e, r, *o):
        if pm.get(n) == pm.get(e):
            call(pm.get(r), [pm.get(x) for x in o])

    def op21(n, e, r, o, *c):
        if abs(_to_num(pm.get(n)) - _to_num(pm.get(e))) > _to_num(pm.get(r)):
            call(pm.get(o), [pm.get(x) for x in c])

    def op22(n, e):
        saved = pm.get(9)
        pm[9] = list(e) if isinstance(e, list) else []
        try:
            run()
        except Exception:
            pass
        pm[n] = _to_str(box["result"])
        pm[9] = saved

    def op23(n, e, *r):
        if pm.get(n) is not None:
            call(pm.get(e), list(r))

    def op24(n, e, r):
        pm[n] = prop(pm.get(e), pm.get(r))

    def op25(*_):
        pass

    def op26(*_):
        pass

    def op27(n, e):
        cur = pm.get(n)
        if isinstance(cur, list):
            try:
                cur.remove(pm.get(e))
            except ValueError:
                pass
        else:
            pm[n] = _to_num(cur) - _to_num(pm.get(e))

    def op28(*_):
        pass

    def op29(n, e, r):
        pm[n] = pm.get(e) < pm.get(r)

    def op30(t, n, e, r):
        is_arr = isinstance(r, list)
        params = (e if is_arr else []) or []
        body = (r if is_arr else e) or []

        def closure(*args):
            saved = pm.get(9)
            if is_arr:
                for idx, reg in enumerate(params):
                    if idx < len(args):
                        pm[reg] = args[idx]
            pm[9] = list(body)
            try:
                run()
            except Exception:
                pass
            res = pm.get(n)
            pm[9] = saved
            return res

        pm[t] = closure

    def op33(n, e, r):
        pm[n] = _to_num(pm.get(e)) * _to_num(pm.get(r))

    def op34(n, e):
        pm[n] = pm.get(e)

    def op35(n, e, r):
        d = _to_num(pm.get(r))
        pm[n] = 0.0 if d == 0 else _to_num(pm.get(e)) / d

    pm[9] = token_list
    pm[10] = "window"
    pm[16] = key
    pm.update({
        0: op0, 1: op1, 2: op2, 3: op3, 4: op4, 5: op5, 6: op6, 7: op7, 8: op8,
        11: op11, 12: op12, 13: op13, 14: op14, 15: op15, 17: op17, 18: op18,
        19: op19, 20: op20, 21: op21, 22: op22, 23: op23, 24: op24,
        25: op25, 26: op26, 27: op27, 28: op28, 29: op29, 30: op30,
        33: op33, 34: op34, 35: op35,
    })

    def run():
        while not box["done"]:
            queue = pm.get(9)
            if not isinstance(queue, list) or not queue:
                break
            instr = queue.pop(0)
            if not isinstance(instr, (list, tuple)) or not instr:
                continue
            fn = pm.get(instr[0])
            if callable(fn):
                try:
                    fn(*instr[1:])
                except Exception:
                    continue

    try:
        run()
    except Exception:
        pass

    return box["result"]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SentinelToken:
    def __init__(self, token: str, proof_token: str = "", turnstile_token: str = ""):
        self.token = token
        self.proof_token = proof_token
        self.turnstile_token = turnstile_token

    def as_headers(self) -> Dict[str, str]:
        headers = {"OpenAI-Sentinel-Chat-Requirements-Token": self.token}
        if self.proof_token:
            headers["OpenAI-Sentinel-Proof-Token"] = self.proof_token
        if self.turnstile_token:
            headers["OpenAI-Sentinel-Turnstile-Token"] = self.turnstile_token
        return headers


class ChatGPTClient:
    """Talks to chatgpt.com directly. No browser involved."""

    def __init__(self, access_token: str = ""):
        self.access_token = access_token
        self.authenticated = bool(access_token)
        self.base = "backend-api" if self.authenticated else "backend-anon"
        self.device_id = new_uuid()
        self.user_agent = AUTH_UA if self.authenticated else ANON_UA
        self._sources: Optional[List[str]] = None
        self._models_cache: Optional[List[Dict[str, Any]]] = None
        self._models_ts = 0.0
        self._models_lock = threading.Lock()

    # -- account -----------------------------------------------------------
    def whoami(self) -> Optional[Dict[str, Any]]:
        """Return the authenticated account (id/email/name) or None."""
        if not self.authenticated:
            return None
        try:
            session = self._session()
            try:
                resp = session.get(f"{BASE_URL}/backend-api/me", timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    return {"id": data.get("id"), "email": data.get("email"), "name": data.get("name"), "plan": data.get("plan_type")}
            finally:
                session.close()
        except Exception as e:
            logger.debug("whoami failed: %s", e)
        return None

    def valid_token(self) -> bool:
        """Cheap check that the current access token is accepted by the backend."""
        if not self.authenticated:
            return False
        try:
            session = self._session()
            try:
                return session.get(f"{BASE_URL}/backend-api/me", timeout=20).status_code == 200
            finally:
                session.close()
        except Exception:
            return False

    # -- models ------------------------------------------------------------
    def list_models(self, ttl: float = 600) -> List[Dict[str, Any]]:
        """Return the account's available models, fetched from the web backend.

        Falls back to a small static list when unavailable (e.g. anonymous mode).
        """
        with self._models_lock:
            if self._models_cache is not None and (time.time() - self._models_ts) < ttl:
                return self._models_cache

            models: List[Dict[str, Any]] = []
            try:
                session = self._session()
                try:
                    resp = session.get(f"{BASE_URL}/{self.base}/models", timeout=20)
                    if resp.status_code == 200:
                        payload = resp.json()
                        for entry in payload.get("models", []):
                            slug = entry.get("slug")
                            if not slug:
                                continue
                            models.append({
                                "id": slug,
                                "object": "model",
                                "created": 1715000000,
                                "owned_by": "openai",
                                "name": entry.get("title") or slug,
                                "description": entry.get("description") or "",
                                "max_tokens": entry.get("max_tokens"),
                            })
                finally:
                    session.close()
            except Exception as e:
                logger.debug("model list fetch failed: %s", e)

            if not models:
                models = [
                    {"id": slug, "object": "model", "created": 1715000000, "owned_by": "openai", "name": slug}
                    for slug in FALLBACK_MODELS
                ]
            elif models[0]["id"] != "auto":
                models.insert(0, {"id": "auto", "object": "model", "created": 1715000000, "owned_by": "openai", "name": "Auto"})

            self._models_cache = models
            self._models_ts = time.time()
            return models

    # -- session -----------------------------------------------------------
    def _session(self) -> requests.Session:
        s = requests.Session(impersonate="firefox133")
        headers = {
            "User-Agent": self.user_agent,
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "OAI-Device-Id": self.device_id,
            "OAI-Language": "en-US",
            "OAI-Client-Version": AUTH_CLIENT_VERSION if self.authenticated else ANON_CLIENT_VERSION,
            "OAI-Client-Build-Number": AUTH_CLIENT_BUILD if self.authenticated else ANON_CLIENT_BUILD,
        }
        if self.authenticated:
            headers["Authorization"] = f"Bearer {self.access_token}"
        s.headers.update(headers)
        s.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
        return s

    def _bootstrap(self, session: requests.Session) -> List[str]:
        if self._sources:
            return self._sources
        resp = session.get(
            BASE_URL + "/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._sources, _ = parse_resources(resp.text)
        return self._sources

    @staticmethod
    def _target_headers(path: str) -> Dict[str, str]:
        return {"X-OpenAI-Target-Path": path, "X-OpenAI-Target-Route": path}

    # -- sentinel ----------------------------------------------------------
    def _sentinel(self, session: requests.Session, sources: List[str]) -> SentinelToken:
        if self.authenticated:
            return self._sentinel_authenticated(session, sources)
        return self._sentinel_anonymous(session, sources)

    def _sentinel_authenticated(self, session: requests.Session, sources: List[str]) -> SentinelToken:
        config = build_pow_config(self.user_agent, sources, "")
        p_token = build_requirements_token(config)

        path = "/backend-api/sentinel/chat-requirements/prepare"
        resp = session.post(
            BASE_URL + path,
            headers={"Content-Type": "application/json", **self._target_headers(path)},
            json={"p": p_token},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        prepare_token = data.get("prepare_token", "")
        if (data.get("arkose") or {}).get("required"):
            raise RuntimeError("sentinel requires arkose token (unsupported)")

        proof_token = ""
        pow_info = data.get("proofofwork") or {}
        if pow_info.get("required"):
            proof_token = build_proof_token(pow_info.get("seed", ""), pow_info.get("difficulty", ""), config)

        turnstile_token = ""
        ts_info = data.get("turnstile") or {}
        if ts_info.get("required") and ts_info.get("dx"):
            turnstile_token = solve_turnstile(ts_info["dx"], p_token, sources) or ""

        path = "/backend-api/sentinel/chat-requirements/finalize"
        resp = session.post(
            BASE_URL + path,
            headers={"Content-Type": "application/json", **self._target_headers(path)},
            json={"prepare_token": prepare_token, "proof_token": proof_token, "turnstile_token": turnstile_token},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("token", "")
        if not token:
            raise RuntimeError(f"missing sentinel token: {resp.text[:300]}")
        return SentinelToken(token, proof_token, turnstile_token)

    def _sentinel_anonymous(self, session: requests.Session, sources: List[str]) -> SentinelToken:
        source = random.choice(sources) if sources else SENTINEL_SDK_FALLBACK
        config = _anon_pow_config(self.user_agent, source, ANON_CLIENT_VERSION)
        p_token = anon_requirements_token(config)

        path = "/backend-anon/sentinel/chat-requirements/prepare"
        resp = session.post(
            BASE_URL + path,
            headers={"Content-Type": "application/json", **self._target_headers(path)},
            json={"p": p_token},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        prepare_token = data.get("prepare_token", "")
        if (data.get("arkose") or {}).get("required"):
            raise RuntimeError("sentinel requires arkose token (unsupported)")

        proof_token = ""
        pow_info = data.get("proofofwork") or {}
        if pow_info.get("required"):
            proof_token = anon_proof_token(pow_info.get("seed", ""), pow_info.get("difficulty", ""), config)

        turnstile_token = ""
        ts_info = data.get("turnstile") or {}
        if ts_info.get("required") and ts_info.get("dx"):
            turnstile_token = solve_turnstile(ts_info["dx"], p_token, sources) or ""

        path = "/backend-anon/sentinel/chat-requirements/finalize"
        resp = session.post(
            BASE_URL + path,
            headers={"Content-Type": "application/json", **self._target_headers(path)},
            json={"prepare_token": prepare_token, "proofofwork": proof_token, "turnstile": turnstile_token},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("token", "")
        if not token:
            raise RuntimeError(f"missing sentinel token: {resp.text[:300]}")
        return SentinelToken(token, proof_token, turnstile_token)

    # -- attachments -------------------------------------------------------
    def _fetch_attachment(self, session: requests.Session, url: str) -> tuple[Optional[bytes], str]:
        """Return (content, mime_type) for an http(s) URL, a data: URI, or a file path."""
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            mime = header.split(";")[0].split(":")[1] or DEFAULT_MIME
            try:
                return base64.b64decode(payload), mime
            except Exception:
                return None, mime
        if url.startswith(("http://", "https://")):
            try:
                resp = session.get(url, timeout=60)
                if resp.status_code != 200:
                    logger.warning("attachment fetch failed (%s): %s", resp.status_code, url)
                    return None, DEFAULT_MIME
                mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip() or DEFAULT_MIME
                return resp.content, mime
            except Exception as e:
                logger.warning("attachment fetch error: %s", e)
                return None, DEFAULT_MIME
        if os.path.exists(url):
            try:
                with open(url, "rb") as f:
                    content = f.read()
                mime = mimetypes.guess_type(url)[0] or DEFAULT_MIME
                return content, mime
            except Exception as e:
                logger.warning("attachment read error: %s", e)
        return None, DEFAULT_MIME

    def upload_file(self, session: requests.Session, content: bytes, mime_type: str) -> Optional[Dict[str, Any]]:
        """Upload one attachment and return its metadata (None on failure)."""
        if not content or not mime_type:
            return None

        width = height = None
        if mime_type.startswith("image/"):
            width, height = image_size(content)
        file_size = len(content)
        file_name = f"{uuid.uuid4()}{file_extension(mime_type)}"
        use_case = determine_use_case(mime_type)

        # 1. ask for an upload slot
        create = session.post(
            f"{BASE_URL}/{self.base}/files",
            headers={"Content-Type": "application/json", "Accept": "*/*"},
            json={
                "file_name": file_name,
                "file_size": file_size,
                "reset_rate_limits": False,
                "timezone_offset_min": -480,
                "use_case": use_case,
            },
            timeout=30,
        )
        if create.status_code != 200:
            logger.error("upload slot failed (%s): %s", create.status_code, create.text[:200])
            return None
        payload = create.json()
        file_id = payload.get("file_id")
        upload_url = payload.get("upload_url")
        if not file_id or not upload_url:
            return None

        # 2. push the bytes to the blob store (no auth headers on this host)
        blob_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": mime_type,
            "x-ms-blob-type": "BlockBlob",
            "x-ms-version": "2020-04-08",
        }
        blob = session.put(upload_url, headers=blob_headers, data=content, timeout=120)
        if blob.status_code not in (200, 201):
            logger.error("blob upload failed (%s): %s", blob.status_code, blob.text[:200])
            return None

        # 3. confirm the upload
        session.post(f"{BASE_URL}/{self.base}/files/{file_id}/uploaded", headers={"Content-Type": "application/json"}, json={}, timeout=30)

        # 4. documents need to finish server-side indexing before use
        if use_case == "my_files":
            for _ in range(30):
                try:
                    check = session.get(f"{BASE_URL}/{self.base}/files/{file_id}", timeout=10)
                    if check.status_code == 200 and check.json().get("retrieval_index_status") == "success":
                        break
                except Exception:
                    pass
                time.sleep(1)

        return {
            "file_id": file_id,
            "file_name": file_name,
            "size_bytes": file_size,
            "mime_type": mime_type,
            "width": width,
            "height": height,
            "use_case": use_case,
        }

    def _user_message(self, session: requests.Session, text: str, attachments: Sequence[str]) -> Dict[str, Any]:
        """Build a user message, uploading any attachments first."""
        if not attachments:
            return self._message(text)
        if not self.authenticated:
            raise RuntimeError("Attachments require authenticated mode (set an access token).")

        parts: List[Any] = []
        if text:
            parts.append(text)
        attachment_meta: List[Dict[str, Any]] = []
        for source in attachments:
            content, mime_type = self._fetch_attachment(session, source)
            if not content:
                logger.warning("skipping attachment: %s", source[:80])
                continue
            meta = self.upload_file(session, content, mime_type)
            if not meta:
                continue
            if mime_type.startswith("image/"):
                parts.append({
                    "content_type": "image_asset_pointer",
                    "asset_pointer": f"file-service://{meta['file_id']}",
                    "size_bytes": meta["size_bytes"],
                    "width": meta["width"],
                    "height": meta["height"],
                })
                attachment_meta.append({
                    "id": meta["file_id"], "size": meta["size_bytes"], "name": meta["file_name"],
                    "mime_type": meta["mime_type"], "width": meta["width"], "height": meta["height"],
                })
            else:
                attachment_meta.append({
                    "id": meta["file_id"], "size": meta["size_bytes"], "name": meta["file_name"],
                    "mime_type": meta["mime_type"],
                })

        if not attachment_meta:
            return self._message(text)

        return {
            "id": new_uuid(),
            "author": {"role": "user"},
            "create_time": time.time(),
            "content": {"content_type": "multimodal_text", "parts": parts},
            "metadata": {"attachments": attachment_meta},
        }

    # -- conversation ------------------------------------------------------
    def _prepare_conversation(self, session: requests.Session, model: str) -> str:
        path = f"/{self.base}/f/conversation/prepare"
        body: Dict[str, Any] = {
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": "client-created-root",
            "model": model,
            "client_prepare_state": "none",
            "timezone_offset_min": -480,
            "timezone": "America/Los_Angeles",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": [],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        resp = session.post(
            BASE_URL + path,
            headers={"Content-Type": "application/json", "Accept": "*/*", "X-Conduit-Token": "no-token", **self._target_headers(path)},
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        conduit_token = str(resp.json().get("conduit_token") or "")
        if not conduit_token:
            raise RuntimeError(f"missing conduit_token: {resp.text[:300]}")
        return conduit_token

    def _message(self, text: str, role: str = "user") -> Dict[str, Any]:
        return {
            "id": new_uuid(),
            "author": {"role": role},
            "create_time": time.time(),
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {},
        }

    @staticmethod
    def _iter_sse(response) -> Iterator[str]:
        for raw in response.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    yield payload

    @staticmethod
    def _extract_text(event: Dict[str, Any], current: str) -> str:
        for candidate in (event, event.get("v")):
            if not isinstance(candidate, dict):
                continue
            msg = candidate.get("message")
            if not isinstance(msg, dict):
                continue
            if (msg.get("author") or {}).get("role") != "assistant":
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            text = "".join(p for p in parts if isinstance(p, str))
            if text:
                return text

        if event.get("p") == "/message/content/parts/0":
            op, v = event.get("o"), str(event.get("v") or "")
            if op == "append":
                return current + v
            if op == "replace":
                return v

        if event.get("o") == "patch" and isinstance(event.get("v"), list):
            text = current
            for item in event["v"]:
                if isinstance(item, dict):
                    text = ChatGPTClient._extract_text(item, text)
            return text

        v = event.get("v")
        if isinstance(v, str) and not event.get("p") and not event.get("o") and current:
            return current + v
        return current

    _MODEL_SLUG_KEYS = ("resolved_model_slug", "model_slug")

    @staticmethod
    def _extract_slug(event: Any) -> Optional[str]:
        """Find the model slug the backend actually used (ignores the `default_model_slug` echo)."""
        if isinstance(event, dict):
            metadata = event.get("metadata")
            if isinstance(metadata, dict):
                for key in ChatGPTClient._MODEL_SLUG_KEYS:
                    slug = metadata.get(key)
                    if isinstance(slug, str) and slug and slug != "auto":
                        return slug
            msg = event.get("message")
            if isinstance(msg, dict):
                found = ChatGPTClient._extract_slug(msg)
                if found:
                    return found
            for value in event.values():
                found = ChatGPTClient._extract_slug(value)
                if found:
                    return found
        elif isinstance(event, list):
            for item in event:
                found = ChatGPTClient._extract_slug(item)
                if found:
                    return found
        return None

    def stream(
        self,
        prompt: str,
        model: str = "auto",
        resolved: Optional[Dict[str, Any]] = None,
        attachments: Optional[Sequence[str]] = None,
    ) -> Iterator[str]:
        """Yield assistant text deltas for a single-turn prompt.

        `attachments` are image/file URLs, `data:` URIs, or local paths; they are uploaded and
        attached to the user message. `resolved`, when given, is filled with
        `{"slug": <model actually used>}` as soon as the backend reports it.
        """
        session = self._session()
        try:
            sources = self._bootstrap(session)
            sentinel = self._sentinel(session, sources)
            conduit_token = self._prepare_conversation(session, model)
            user_message = self._user_message(session, prompt, attachments or [])

            path = f"/{self.base}/f/conversation"
            payload: Dict[str, Any] = {
                "action": "next",
                "messages": [user_message],
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "America/Los_Angeles",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
            }
            headers = {
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "X-Conduit-Token": conduit_token,
                **sentinel.as_headers(),
                **self._target_headers(path),
            }
            resp = session.post(BASE_URL + path, headers=headers, json=payload, timeout=300, stream=True)
            resp.raise_for_status()

            current = ""
            try:
                for payload_str in self._iter_sse(resp):
                    if payload_str == "[DONE]":
                        break
                    try:
                        event = json.loads(payload_str)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") in ("stream_handoff", "resume_conversation_token", "delta_encoding"):
                        continue
                    error = event.get("error")
                    if error:
                        code = event.get("error_code") or "upstream_error"
                        raise RuntimeError(f"{error} ({code})")
                    if resolved is not None and not resolved.get("slug"):
                        slug = self._extract_slug(event)
                        if slug:
                            resolved["slug"] = slug
                    new_text = self._extract_text(event, current)
                    if new_text != current:
                        yield new_text[len(current):]
                        current = new_text
            finally:
                resp.close()
        finally:
            session.close()
