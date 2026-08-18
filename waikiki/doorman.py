"""Optional integration with Doorman, if the user happens to run it.

Doorman is a sibling local app (same pywebview + FastAPI shape) that already has
things Waikiki does less well — much better speech than the browser's
`speechSynthesis`, and remote/local agents with model access the user has
already configured there.

**Strictly optional, and quiet about it.** Doorman's own rule is that a feature
must never require an out-of-app action, with sibling-app integrations as the
explicit exception provided they stay optional. Waikiki holds itself to the same
line: everything here degrades to what Waikiki already does, nothing is
installed or launched on the user's behalf, and if Doorman is not running we say
nothing.

Two things this module owns:

*Detection.* A health check against Doorman's local port, cached both ways so a
machine without Doorman costs one short-timeout request rather than one per
page. `/api/health` also carries cheap `capabilities` hints, and each capability
has its own `/api/{name}/status` probe — **capability probing, never version
sniffing**: an older Doorman 404s those routes and a 404 is treated exactly like
the documented quiet `{"available": false}` 200.

*Routing.* Which backend answers generation, chat and images. That decision
lives here, below the routes, so the HTML views, the REST API and the MCP tools
all agree on it (CLAUDE.md rule 5) — and every caller is handed a label saying
which backend answered, because silently swapping the user's configured model
for a Doorman agent would be its own bug.

One exception to "optional": when Waikiki is being displayed **inside Doorman's
own window**, Doorman is unambiguously present and is the host, so offering the
integration as a checkbox is incoherent. `embedded()` detects that and
`enabled()` forces the integration on; Settings renders the control locked, with
the reason, rather than making it disappear.
"""
from __future__ import annotations

import json
import os
import time
from typing import AsyncIterator, Iterator

from . import store

# Doorman's default; DOORMAN_PORT overrides it there, so honour the same name.
BASE = os.environ.get("WAIKIKI_DOORMAN_URL") or \
    f"http://127.0.0.1:{os.environ.get('DOORMAN_PORT', '8900')}"

# Long enough that a page load never pays for a second probe, short enough that
# starting Doorman is noticed without a restart.
_TTL = 30.0
_cache: dict = {"at": 0.0, "info": None}

# Per-capability probes (/api/ask/status, /api/image/status), cached both ways
# for the same reason and on the same clock as the health probe.
_caps: dict[str, dict] = {}

# When we last saw a request that was a Doorman-framed document. Refreshed by
# every navigation inside the frame, so browsing keeps it warm; it expires so a
# frame closed hours ago doesn't keep claiming to be the host.
#
# Process-level rather than per-request because the evidence only arrives on the
# *document* request (`Sec-Fetch-Dest: iframe`); the fetch/XHR calls the framed
# page then makes carry no framing header at all, and they need the same answer.
# Unlike the active wiki (CLAUDE.md rule 4) this is a property of the host
# window, not of a caller, so there is nothing here to leak between agents.
_EMBED_TTL = 900.0
_embed: dict = {"at": 0.0}

# Doorman's iframe carries no marker today, so `Sec-Fetch-Dest` is the signal.
# If a future Doorman appends one, honour it too: it is strictly better evidence.
EMBED_MARKER = "doorman"
_FRAME_DESTS = ("iframe", "frame", "embed", "object")


def refresh() -> None:
    """Drop every cached probe, so the next question re-asks Doorman. Used after
    a settings change, where waiting up to 30s to see the effect is confusing.
    Leaves the framing memory alone: whether Doorman is displaying this window is
    not something a settings save has any news about."""
    _cache.update(at=0.0, info=None)
    _caps.clear()


def forget() -> None:
    """`refresh()`, and forget the framing too. Tests use this to start from a
    world where Doorman has never been seen."""
    refresh()
    _embed.update(at=0.0)


# --- detection ---------------------------------------------------------------

def preference() -> bool:
    """What the user asked for in Settings. On by default, but it only ever
    *offers* — nothing here works unless Doorman is actually running."""
    return store.get_setting("doorman_enabled", "1") == "1"


def _get(path: str, timeout: float = 0.6):
    import httpx

    with httpx.Client(timeout=timeout) as c:
        r = c.get(BASE + path)
        r.raise_for_status()
        return r.json()


def _probe(force: bool = False) -> dict | None:
    """Doorman's health payload, or None when it isn't there — the raw look, with
    no preference gate, so `embedded()` can ask "is Doorman really the host?"
    even for a user who had switched the integration off.

    Cached both ways: a negative result is cached too, so a machine without
    Doorman doesn't make a doomed request on every render.
    """
    now = time.monotonic()
    if not force and (now - _cache["at"]) < _TTL:
        return _cache["info"]
    try:
        data = _get("/api/health")
        _cache.update(at=now, info=data if data.get("ok") else None)
    except Exception:
        _cache.update(at=now, info=None)     # not running: entirely normal
    return _cache["info"]


def running() -> bool:
    """Is Doorman there at all, regardless of whether we're allowed to use it."""
    return _probe() is not None


def looks_framed(headers: dict | None = None, query: str = "") -> bool:
    """Does this request say it is a document loading inside a frame?

    Pure and free: browsers send `Sec-Fetch-Dest: iframe` on the navigation that
    loads a frame, which is the one signal available server-side without asking
    Doorman to change anything. A `?embed=doorman` marker is honoured too, for a
    future Doorman that adds one — it is strictly better evidence.

    This says *an* iframe, not *Doorman's* iframe. `note_request` is what turns
    it into the stronger claim.
    """
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    dest = (h.get("sec-fetch-dest") or "").lower()
    return dest in _FRAME_DESTS or f"embed={EMBED_MARKER}" in (query or "").lower()


def note_request(headers: dict | None = None, query: str = "") -> None:
    """Record whether this request looks like Doorman framing Waikiki.

    Called by the middleware for framed document requests only, so standalone
    Waikiki never pays for it.

    "Embedded" must mean embedded **in Doorman**, not in any iframe — a random
    page framing Waikiki must not be able to switch the integration on — so the
    framing signal is only accepted when Doorman itself answers its health check.
    """
    if not looks_framed(headers, query):
        return
    if running():
        _embed["at"] = time.monotonic()


def _framed_recently() -> bool:
    at = _embed["at"]
    return bool(at) and (time.monotonic() - at) <= _EMBED_TTL


def embedded() -> bool:
    """True when Waikiki is being displayed inside Doorman's own window."""
    return _framed_recently() and running()


def enabled() -> bool:
    """Whether the integration is on. The user's choice — except when Doorman is
    the host, where "optional" has no meaning and the answer is yes."""
    return embedded() or preference()


def info(force: bool = False) -> dict | None:
    """Doorman's health payload if we're allowed to use it, else None."""
    if not enabled():
        return None
    return _probe(force)


def available() -> bool:
    return info() is not None


# --- capabilities ------------------------------------------------------------

def _capability(name: str) -> dict:
    """`/api/{name}/status`, cached both ways.

    A Doorman that predates the capability 404s the route. That is not an error
    and not a version to sniff — it is exactly the documented quiet
    `{"available": false, "reason": ...}` 200, and is treated identically.
    """
    if not available():
        return {"available": False, "reason": "Doorman isn't running"}
    hint = ((info() or {}).get("capabilities") or {})
    if name in hint and not hint[name]:
        # Free "no" from the health payload we already have.
        return {"available": False, "reason": f"no {name} configured in Doorman"}
    now = time.monotonic()
    got = _caps.get(name)
    if got and (now - got["at"]) < _TTL:
        return got["val"]
    try:
        val = _get(f"/api/{name}/status", timeout=1.5)
        if not isinstance(val, dict):
            val = {"available": False, "reason": "unexpected reply"}
    except Exception:
        # 404 (older Doorman), a refused connection, anything: not available.
        val = {"available": False, "reason": f"this Doorman has no {name} capability"}
    _caps[name] = {"at": now, "val": val}
    return val


def ask_status() -> dict:
    """Can Doorman answer a one-shot question, and with which agent?"""
    return _capability("ask")


def image_status() -> dict:
    """Can Doorman render an image, and with which model?"""
    return _capability("image")


def can_ask() -> bool:
    return bool(ask_status().get("available"))


def can_image() -> bool:
    return bool(image_status().get("available"))


def ask_backend() -> dict | None:
    """The label for "a Doorman agent answered this", or None if it can't."""
    st = ask_status()
    if not st.get("available"):
        return None
    agent = st.get("agent") or st.get("platform") or "agent"
    return {"backend": "doorman", "agent": agent,
            "label": f"Doorman · {agent}"}


def image_backend() -> dict | None:
    st = image_status()
    if not st.get("available"):
        return None
    model = st.get("model") or "image model"
    return {"backend": "doorman", "model": model, "label": f"Doorman · {model}"}


# --- one-shot ask (POST /api/ask) -------------------------------------------
# Doorman answers with an NDJSON stream of bridge events rather than a string:
# the same governed path as its own /send, so per-agent grants and confirm/go
# still apply on its side. We read the agent's message events and ignore the
# rest of its bookkeeping.

def _ask_body(text: str, wiki: str, page: str, agent: str) -> dict:
    body: dict = {"text": text}
    if agent:
        body["agent"] = agent
    # Doorman keys an audited conversation per (wiki, page, agent). Sending
    # these is what keeps those threads separate and legible over there.
    if wiki:
        body["wiki"] = wiki
    if page:
        body["page"] = page
    return body


def _quiet(payload) -> dict:
    """Doorman's "not configured" 200 → our unavailable event."""
    reason = ""
    if isinstance(payload, dict):
        reason = payload.get("reason") or payload.get("error") or ""
    return {"kind": "unavailable", "reason": reason or "no agent configured"}


def _ask_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        evt = json.loads(line)
    except ValueError:
        return None
    return evt if isinstance(evt, dict) else None


def _is_answer(evt: dict) -> bool:
    return evt.get("kind") == "message" and evt.get("role") == "agent"


def ask_events(text: str, wiki: str = "", page: str = "", agent: str = "",
               timeout: float = 300.0) -> Iterator[dict]:
    """Stream Doorman's bridge events for one question. Never raises.

    Yields Doorman's own events, plus a synthetic `{"kind": "unavailable"}` when
    Doorman can't answer — the caller's cue to fall back to its local path
    without showing the user anything.
    """
    import httpx

    if not (text or "").strip() or not can_ask():
        yield _quiet(None)
        return
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0)) as c:
            with c.stream("POST", BASE + "/api/ask",
                          json=_ask_body(text, wiki, page, agent)) as r:
                ctype = (r.headers.get("content-type") or "").lower()
                if r.status_code != 200 or "ndjson" not in ctype:
                    # 404 = older Doorman; JSON 200 = quiet "not configured";
                    # 4xx/5xx = a real failure we still fall back from.
                    payload = None
                    try:
                        payload = json.loads(r.read().decode("utf-8", "replace"))
                    except Exception:
                        payload = None
                    yield _quiet(payload)
                    return
                for line in r.iter_lines():
                    evt = _ask_line(line)
                    if evt is None:
                        continue
                    yield evt
                    if evt.get("kind") == "end":
                        return
    except Exception as exc:
        yield {"kind": "unavailable", "reason": str(exc)[:200]}


async def ask_events_async(text: str, wiki: str = "", page: str = "",
                           agent: str = "",
                           timeout: float = 300.0) -> AsyncIterator[dict]:
    """`ask_events` for async callers (the generation SSE endpoint)."""
    import httpx

    if not (text or "").strip() or not can_ask():
        yield _quiet(None)
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as c:
            async with c.stream("POST", BASE + "/api/ask",
                                json=_ask_body(text, wiki, page, agent)) as r:
                ctype = (r.headers.get("content-type") or "").lower()
                if r.status_code != 200 or "ndjson" not in ctype:
                    try:
                        payload = json.loads((await r.aread()).decode("utf-8", "replace"))
                    except Exception:
                        payload = None
                    yield _quiet(payload)
                    return
                async for line in r.aiter_lines():
                    evt = _ask_line(line)
                    if evt is None:
                        continue
                    yield evt
                    if evt.get("kind") == "end":
                        return
    except Exception as exc:
        yield {"kind": "unavailable", "reason": str(exc)[:200]}


def ask(text: str, wiki: str = "", page: str = "", agent: str = "",
        timeout: float = 300.0) -> dict | None:
    """One question, one answer, through a Doorman agent.

    Returns None when Doorman can't answer — "use your own path, say nothing".
    Otherwise `{"ok": True, "answer", "backend", "label"}`, or `{"ok": False,
    "error"}` when Doorman was there, tried, and failed (that one it means to be
    visible).
    """
    parts: list[str] = []
    errors: list[str] = []
    for evt in ask_events(text, wiki, page, agent, timeout):
        kind = evt.get("kind")
        if kind == "unavailable":
            return None
        if _is_answer(evt):
            body = (evt.get("text") or "").strip()
            if body:
                parts.append(body)
        elif kind == "error":
            errors.append((evt.get("text") or "").strip())
    answer = "\n\n".join(parts).strip()
    label = (ask_backend() or {}).get("label", "Doorman")
    if not answer:
        if errors:
            return {"ok": False, "error": f"{label}: {errors[0][:400]}"}
        return None                     # nothing said and nothing wrong: fall back
    return {"ok": True, "answer": answer, "backend": "doorman", "label": label}


# --- images (POST /api/image) ------------------------------------------------

def image(prompt: str, model: str = "", size: str = "",
          timeout: float = 300.0) -> dict | None:
    """Render one image through Doorman.

    None means "Doorman can't do this" — fall back to the local CLI, quietly.
    `{"ok": False, "error"}` means Doorman tried and failed, which it signals
    deliberately (400/502) and the user should see.
    """
    import base64

    import httpx

    if not (prompt or "").strip() or not can_image():
        return None
    body: dict = {"prompt": prompt, "json": True}
    if model:
        body["model"] = model
    if size:
        body["size"] = size
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0)) as c:
            r = c.post(BASE + "/api/image", json=body)
            if r.status_code == 404:
                return None                     # older Doorman: no such route
            try:
                payload = r.json()
            except Exception:
                payload = {}
            if r.status_code != 200:
                reason = payload.get("error") or f"HTTP {r.status_code}"
                return {"ok": False, "error": f"Doorman image: {reason}"}
            if not payload.get("available"):
                return None                     # quiet "not configured"
            data = base64.b64decode(payload.get("b64") or "")
            if not data:
                return {"ok": False, "error": "Doorman returned an empty image."}
            used = payload.get("model") or model or "image model"
            return {"ok": True, "data": data,
                    "mime": payload.get("mime") or "image/png",
                    "model": used, "backend": "doorman",
                    "label": f"Doorman · {used}"}
    except Exception:
        return None                             # unreachable: fall back quietly


# --- speech (POST /api/tts) --------------------------------------------------

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
    ask_be = ask_backend() if got else None
    img_be = image_backend() if got else None
    return {
        "enabled": enabled(),
        "preference": preference(),
        "embedded": embedded(),
        "running": got is not None,
        "url": BASE,
        "version": (got or {}).get("version"),
        "voices": (voices() or {}).get("profiles") or [],
        "ask": ask_be,
        "image": img_be,
    }
