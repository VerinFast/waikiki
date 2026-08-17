"""Optional integration with Doorman, if the user happens to run it.

Doorman is a sibling local app (same pywebview + FastAPI shape) that already has
things Waikiki does less well — notably much better speech than the browser's
`speechSynthesis`, and remote/local agents behind a tool bridge.

**Strictly optional, and quiet about it.** Doorman's own rule is that a feature
must never require an out-of-app action, with sibling-app integrations as the
explicit exception provided they stay optional. Waikiki holds itself to the same
line: everything here degrades to what Waikiki already does, nothing is
installed or launched on the user's behalf, and if Doorman is not running we say
nothing.

Detection is a health check against its local port, cached so a missing Doorman
costs one short-timeout request rather than one per page.
"""
from __future__ import annotations

import os
import time

from . import store

# Doorman's default; DOORMAN_PORT overrides it there, so honour the same name.
BASE = os.environ.get("WAIKIKI_DOORMAN_URL") or \
    f"http://127.0.0.1:{os.environ.get('DOORMAN_PORT', '8900')}"

# Long enough that a page load never pays for a second probe, short enough that
# starting Doorman is noticed without a restart.
_TTL = 30.0
_cache: dict = {"at": 0.0, "info": None}


def enabled() -> bool:
    """Whether the user wants the integration at all. On by default, but only
    ever *offers* — nothing here works unless Doorman is actually running."""
    return store.get_setting("doorman_enabled", "1") == "1"


def _get(path: str, timeout: float = 0.6):
    import httpx

    with httpx.Client(timeout=timeout) as c:
        r = c.get(BASE + path)
        r.raise_for_status()
        return r.json()


def info(force: bool = False) -> dict | None:
    """Doorman's health payload, or None when it isn't there.

    Cached both ways: a negative result is cached too, so a machine without
    Doorman doesn't make a doomed request on every render.
    """
    if not enabled():
        return None
    now = time.monotonic()
    if not force and (now - _cache["at"]) < _TTL:
        return _cache["info"]
    try:
        data = _get("/api/health")
        _cache.update(at=now, info=data if data.get("ok") else None)
    except Exception:
        _cache.update(at=now, info=None)     # not running: entirely normal
    return _cache["info"]


def available() -> bool:
    return info() is not None


def voices() -> dict:
    """Doorman's speech profiles: {available, profiles, default}, or empty."""
    if not available():
        return {}
    try:
        return _get("/api/tts/status") or {}
    except Exception:
        return {}


def speak(text: str, voice: str = "") -> bytes | None:
    """Synthesise `text` through Doorman. Returns WAV bytes, or None.

    None means "use the browser's own voice" — never an error the reader has to
    care about, because this is a nicer-sounding path, not a required one.
    """
    if not (text or "").strip() or not available():
        return None
    try:
        import httpx

        body = {"text": text}
        if voice:
            body["voice"] = voice
        with httpx.Client(timeout=60.0) as c:
            r = c.post(BASE + "/api/tts", json=body)
            if r.status_code != 200:
                return None
            return r.content
    except Exception:
        return None


def status() -> dict:
    """What Settings shows. Never raises."""
    got = info()
    return {
        "enabled": enabled(),
        "running": got is not None,
        "url": BASE,
        "version": (got or {}).get("version"),
        "voices": (voices() or {}).get("profiles") or [],
    }
