"""Bonjour / mDNS advertisement, so a shared Waikiki is discoverable on the LAN.

Uses macOS's built-in ``dns-sd`` rather than a Python mDNS library: it needs no
new dependency, nothing to bundle, and it's the same responder the OS already
uses. We advertise ``_http._tcp`` so the wiki shows up in Bonjour-aware browsers
and in ``dns-sd -B _http._tcp``.

Only runs while LAN sharing is on — if the wiki isn't reachable from the network
there's nothing worth advertising. The helper process is a child of the app and
is torn down with it.
"""
from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import threading

_proc: subprocess.Popen | None = None
_lock = threading.Lock()

SERVICE_TYPE = "_http._tcp"


def available() -> bool:
    return bool(shutil.which("dns-sd"))


def _reap_orphans(name: str) -> None:
    """Kill a stale advertiser left behind by a previous run.

    If the app was force-killed (SIGKILL), neither atexit nor our shutdown hook
    ran, so an old `dns-sd -R` can survive and keep announcing a wiki that is no
    longer listening. Matching is scoped to our exact service registration."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"dns-sd -R {name} {SERVICE_TYPE}"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return
    import os
    import signal
    for pid in (p.strip() for p in out.splitlines()):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass


def start(port: int, name: str = "Waikiki") -> bool:
    """Advertise the wiki on the LAN. Idempotent; returns True if advertising."""
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True
        if not available():
            return False
        _reap_orphans(name)
        try:
            # -R = register a service:  <name> <type> <domain> <port> [TXT...]
            # No start_new_session: keeping the child in our process group means a
            # group-level kill takes the advertiser down with the app.
            _proc = subprocess.Popen(
                ["dns-sd", "-R", name, SERVICE_TYPE, "local.", str(port),
                 "path=/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
            atexit.register(stop)
            return True
        except Exception as exc:
            print(f"[waikiki] bonjour advertise failed: {exc}", file=sys.stderr)
            _proc = None
            return False


def stop() -> None:
    """Withdraw the advertisement (killing dns-sd deregisters the service)."""
    global _proc
    with _lock:
        p, _proc = _proc, None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None
