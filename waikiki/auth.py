"""LAN sharing: password auth and the owner/guest role split.

Waikiki normally binds to loopback and needs no auth. Turning on sharing binds it
to the LAN, so we add a password — and, importantly, a capability split:

* **owner**  — requests from loopback (the person sitting at the machine). Full
  access, exactly as before.
* **guest**  — someone on the network who entered the password. Can read and edit
  pages, but *not* reach anything that runs a local command or reconfigures the
  host.

That second point is the whole reason roles exist rather than one password: the
Settings page lets you set `image_cli` to any command name, and the image/chat
endpoints execute it on this machine. Without the split, sharing the wiki
password would be equivalent to handing out shell access.

This is deliberately lightweight (a shared password over plain HTTP on a trusted
LAN) — it is not hardened multi-user auth, and it is off by default.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from . import appconfig

COOKIE = "waikiki_share"
_ITERATIONS = 200_000
SESSION_DAYS = 14

# Paths a guest may never reach. Two categories:
#   1. executes a local command on the host  (chat, image generation)
#   2. reconfigures the host or leaks local detail (settings, wikis, logs, connect)
_GUEST_DENY_EXACT = {
    "/settings", "/elements", "/elements/new", "/elements/save",
    "/wikis", "/wikis/create", "/wikis/import", "/connect", "/logs",
    "/logs/clear", "/debug", "/debug/clear", "/settings/style-refs",
    "/settings/models/add", "/settings/models/activate",
}
_GUEST_DENY_PREFIX = ("/wikis/", "/elements/", "/settings/", "/debug", "/logs")
_GUEST_DENY_SUFFIX = ("/chat", "/generate-image", "/purge")


def _cfg(key, default=None):
    return appconfig.get(key, default)


def enabled() -> bool:
    """True when remote callers may reach this wiki at all — LAN sharing on, or a
    public tunnel running. Either way a password must be set."""
    if not _cfg("share_hash"):
        return False
    if bool(_cfg("share_enabled")):
        return True
    from . import tunnel          # lazy: tunnel imports nothing from auth
    return tunnel.is_running()


def has_password() -> bool:
    return bool(_cfg("share_hash"))


def _secret() -> bytes:
    """Server-side signing key for session cookies (generated once)."""
    s = _cfg("share_secret")
    if not s:
        s = secrets.token_hex(32)
        appconfig.set("share_secret", s)
    return s.encode()


def set_password(password: str) -> None:
    """Store a salted PBKDF2 hash. An empty password clears it (disables sharing)."""
    if not (password or "").strip():
        appconfig.set("share_hash", "")
        appconfig.set("share_salt", "")
        appconfig.set("share_enabled", False)
        return
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                                 _ITERATIONS).hex()
    appconfig.set("share_salt", salt)
    appconfig.set("share_hash", digest)
    # Rotate the signing key so existing sessions don't survive a password change.
    appconfig.set("share_secret", secrets.token_hex(32))


def verify_password(password: str) -> bool:
    stored, salt = _cfg("share_hash"), _cfg("share_salt")
    if not stored or not salt:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(),
                                 bytes.fromhex(salt), _ITERATIONS).hex()
    return hmac.compare_digest(digest, stored)


def make_token(days: int = SESSION_DAYS) -> str:
    exp = str(int(time.time()) + days * 86400)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(token: str) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
        if int(exp) < time.time():
            return False
        want = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want)
    except Exception:
        return False


# Headers a reverse proxy / tunnel adds. Their presence means the request did
# NOT originate on this machine, even though it arrives from 127.0.0.1.
_PROXY_HEADERS = ("x-forwarded-for", "cf-connecting-ip", "forwarded",
                  "cf-ray", "x-real-ip", "cf-ipcountry")


def is_proxied(headers: dict | None) -> bool:
    return any(h in (headers or {}) for h in _PROXY_HEADERS)


def is_local(client_host: str, headers: dict | None = None) -> bool:
    """Owner access = physically on this machine.

    A tunnel (cloudflared) connects to the server over loopback, so the client
    address alone would wrongly mark internet traffic as the owner — which would
    publish Settings and the command-running endpoints to the world. Any request
    carrying proxy/forwarding headers is therefore never treated as local."""
    if is_proxied(headers):
        return False
    return client_host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def guest_may(path: str) -> bool:
    """False for anything that runs a local command or reconfigures the host."""
    p = (path or "/").rstrip("/") or "/"
    if p in _GUEST_DENY_EXACT:
        return False
    if any(p.endswith(s) for s in _GUEST_DENY_SUFFIX):
        return False
    if any(p.startswith(pre) for pre in _GUEST_DENY_PREFIX):
        return False
    return True


def lan_urls(port: int) -> list[str]:
    """Best-effort LAN addresses to hand out. Never raises."""
    import socket
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no packets sent; just picks the route
        out.append(f"http://{s.getsockname()[0]}:{port}")
        s.close()
    except Exception:
        pass
    try:
        host = socket.gethostname()
        if host and not host.endswith(".local"):
            host += ".local"
        if host:
            out.append(f"http://{host}:{port}")
    except Exception:
        pass
    return out


def share_lan_enabled() -> bool:
    """Whether the server should bind beyond loopback (read at startup)."""
    return bool(_cfg("share_enabled")) and bool(_cfg("share_hash"))
