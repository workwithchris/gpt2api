"""Read a ChatGPT access token from a locally logged-in browser (macOS)."""

from typing import Optional


def read_local_browser_token() -> Optional[str]:
    """Return the access token from the machine's logged-in Chrome/Arc, or raise RuntimeError."""
    try:
        import browser_cookie3
    except ImportError:
        raise RuntimeError("browser_cookie3 not installed. Run: pip install browser_cookie3")

    import curl_cffi.requests as requests

    jar = []
    for reader in (browser_cookie3.arc, browser_cookie3.chrome):
        try:
            found = list(reader(domain_name="chatgpt.com"))
        except Exception:
            continue
        if any(c.name.startswith("__Secure-next-auth.session-token") for c in found):
            jar = found
            break

    if not jar:
        raise RuntimeError("No logged-in ChatGPT session found in a local browser.")

    # Plain urllib is blocked by Cloudflare; use the same TLS fingerprint as the API client.
    session = requests.Session(impersonate="firefox133")
    for cookie in jar:
        session.cookies.set(cookie.name, cookie.value, domain="chatgpt.com")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
    })
    try:
        resp = session.get("https://chatgpt.com/api/auth/session", timeout=30)
    finally:
        session.close()
    if resp.status_code != 200:
        raise RuntimeError(f"ChatGPT rejected the session request (HTTP {resp.status_code}).")

    token = (resp.json().get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("Session found but no access token (expired login?).")
    return token
