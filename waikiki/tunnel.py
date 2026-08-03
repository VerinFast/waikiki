"""Temporary public access to this wiki via a Cloudflare Quick Tunnel.

Runs ``cloudflared tunnel --url http://127.0.0.1:<port>`` and scrapes the
``*.trycloudflare.com`` address it prints. That address is HTTPS and lives only
as long as the process, which suits the intended flow: start a tunnel, send the
URL and the password to someone out of band, stop it when you're done.

Two safety rules are enforced here rather than left to the caller:

1. **A password is required.** A tunnel makes the wiki reachable from the whole
   internet; starting one without a password would publish it anonymously.
2. **Tunnel traffic is never "owner" traffic.** cloudflared connects over
   loopback, so requests would otherwise look local and get full access —
   ``auth.is_local()`` rejects anything carrying proxy headers for that reason.

This is deliberately a *quick* tunnel: no Cloudflare account, no DNS, ephemeral
URL. It is not a hardened public deployment.
"""
from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import sys
import threading
import time

_proc: subprocess.Popen | None = None
_url: str | None = None
_lock = threading.Lock()
_started_at: float | None = None

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
STARTUP_TIMEOUT = 45


def available() -> bool:
    return bool(shutil.which("cloudflared"))


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def url() -> str | None:
    return _url if is_running() else None


def started_at() -> float | None:
    return _started_at if is_running() else None


def start(port: int) -> dict:
    """Open a public tunnel to the local server. Returns {ok, url|error}."""
    global _proc, _url, _started_at
    with _lock:
        if is_running():
            return {"ok": True, "url": _url, "already": True}
        if not available():
            return {"ok": False, "error":
                    "cloudflared isn't installed. Install it (brew install "
                    "cloudflared) and try again."}
        from . import auth
        if not auth.has_password():
            return {"ok": False, "error":
                    "Set a sharing password first — a tunnel puts this wiki on "
                    "the public internet, so it must not be open to anyone."}
        try:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}",
                 "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception as exc:
            return {"ok": False, "error": f"Couldn't start cloudflared: {exc}"}

        found: dict = {}

        def _read():
            # cloudflared prints the assigned URL to stderr (merged into stdout).
            for line in proc.stdout:                      # type: ignore[union-attr]
                m = _URL_RE.search(line)
                if m and "url" not in found:
                    found["url"] = m.group(0)
                    break
            # Drain the rest so the pipe never fills and blocks cloudflared.
            try:
                for _ in proc.stdout:                      # type: ignore[union-attr]
                    pass
            except Exception:
                pass

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline and "url" not in found and proc.poll() is None:
            time.sleep(0.25)

        if "url" not in found:
            try:
                proc.terminate()
            except Exception:
                pass
            return {"ok": False, "error":
                    "cloudflared didn't return a URL in time. Check the network "
                    "and try again."}

        _proc, _url, _started_at = proc, found["url"], time.time()
        atexit.register(stop)
        return {"ok": True, "url": _url}


def stop() -> None:
    global _proc, _url, _started_at
    with _lock:
        p, _proc, _url, _started_at = _proc, None, None, None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        except KeyboardInterrupt:  # pragma: no cover
            pass
    if p is not None:
        print("[waikiki] public tunnel closed", file=sys.stderr)
