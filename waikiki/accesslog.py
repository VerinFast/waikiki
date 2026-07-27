"""Access log: every request received and every call made, one line each.

Format is Common/Combined Log Format with two ELF-style trailing extensions
(duration in ms, then a free-text detail field):

    HOST - - [10/Oct/2026:13:55:36 -0700] "REQUEST" STATUS SIZE "REFERER" "AGENT" 12ms DETAIL

The REQUEST field's first token identifies the kind of event:
    GET/POST/…  an HTTP request served by the web app
    MCP         an MCP tool call (Claude Cowork / Desktop)
    CLI         a local CLI we spawned (claude / agy / gemini)
    ERROR       an unhandled Python error (detail carries the exception)

Files are hour-stamped (``access-YYYY-MM-DD-HH.log``) so rotation is implicit and
safe across the web + MCP processes that share the data dir — no renames. Files
older than the retention window (default 24h, set in app config) are swept.
"""
from __future__ import annotations

import datetime
import logging
import re
import threading
import traceback

from . import appconfig, config

_lock = threading.Lock()
DEFAULT_RETENTION_HOURS = 24
_last_sweep = 0.0

_LINE = re.compile(
    r'^(?P<host>\S+) - - \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
    r'(?P<status>\S+) (?P<size>\S+) "(?P<referer>[^"]*)" "(?P<agent>[^"]*)"'
    r'(?: (?P<ms>\d+)ms)?(?: (?P<detail>.*))?$'
)


def _logs_dir():
    d = config.DATA_DIR / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now():
    return datetime.datetime.now().astimezone()


def _file_for(when):
    return _logs_dir() / f"access-{when:%Y-%m-%d-%H}.log"


def retention_hours() -> int:
    try:
        return max(1, int(appconfig.get("log_retention_hours", DEFAULT_RETENTION_HOURS)))
    except Exception:
        return DEFAULT_RETENTION_HOURS


def _q(s: str) -> str:
    return (s or "-").replace('"', "'").replace("\n", " ").replace("\r", " ")


def record(request: str, status="-", size=0, dur_s: float = 0.0, host: str = "-",
           referer: str = "-", agent: str = "-", detail: str = "") -> None:
    """Write one access-log line. Never raises."""
    try:
        now = _now()
        clf = now.strftime("%d/%b/%Y:%H:%M:%S %z")
        ms = int(max(0.0, dur_s) * 1000)
        line = (f'{host or "-"} - - [{clf}] "{_q(request)}" {status} '
                f'{size if size else "-"} "{_q(referer)}" "{_q(agent)}" {ms}ms')
        if detail:
            line += " " + _q(detail)[:600]
        path = _file_for(now)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        _maybe_sweep()
    except Exception:
        pass  # logging must never break the request


def http(host, method, path, proto, status, size, dur_s, agent="-", referer="-"):
    record(f"{method} {path} {proto}", status=status, size=size, dur_s=dur_s,
           host=host or "-", referer=referer, agent=agent)


def mcp(tool: str, ok: bool, dur_s: float, detail: str = ""):
    record(f"MCP {tool}", status=200 if ok else 500, dur_s=dur_s, host="mcp",
           agent="mcp", detail=detail)


def cli(label: str, returncode, dur_s: float):
    record(f"CLI {label}", status=returncode if returncode is not None else "timeout",
           dur_s=dur_s, host="cli", agent="cli")


def error(context: str, exc: BaseException, dur_s: float = 0.0):
    tb = exc.__traceback__
    where = ""
    if tb:
        last = traceback.extract_tb(tb)[-1]
        where = f" @ {last.filename.split('/')[-1]}:{last.lineno} in {last.name}"
    record(f"ERROR {context}", status=500, dur_s=dur_s,
           detail=f"{type(exc).__name__}: {exc}{where}")


def _maybe_sweep():
    global _last_sweep
    import time
    if time.time() - _last_sweep < 60:
        return
    _last_sweep = time.time()
    sweep()


def sweep() -> int:
    """Delete hour-files older than the retention window. Returns count removed."""
    cutoff = _now() - datetime.timedelta(hours=retention_hours())
    removed = 0
    for f in _logs_dir().glob("access-*.log"):
        m = re.search(r"access-(\d{4})-(\d{2})-(\d{2})-(\d{2})\.log$", f.name)
        if not m:
            continue
        y, mo, d, h = map(int, m.groups())
        try:
            when = datetime.datetime(y, mo, d, h, tzinfo=_now().tzinfo)
        except ValueError:
            continue
        if when < cutoff:
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
    return removed


def _parse(line: str):
    m = _LINE.match(line)
    if not m:
        return None
    d = m.groupdict()
    ts_iso, epoch = "", 0.0
    try:
        dt = datetime.datetime.strptime(d["time"], "%d/%b/%Y:%H:%M:%S %z")
        ts_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
        epoch = dt.timestamp()
    except Exception:
        pass
    req = d["request"] or ""
    first = req.split(" ", 1)[0].upper()
    kind = first if first in ("MCP", "CLI", "ERROR") else "HTTP"
    return {
        "ts": ts_iso, "epoch": epoch, "kind": kind, "host": d["host"],
        "request": req, "status": d["status"], "size": d["size"],
        "ms": int(d["ms"]) if d["ms"] else 0, "detail": d["detail"] or "",
        "raw": line,
    }


def read(limit: int = 500, query: str = "", kind: str = "") -> list[dict]:
    """Most-recent-first parsed entries, optionally filtered by substring/kind."""
    files = sorted(_logs_dir().glob("access-*.log"))
    q = (query or "").lower()
    out: list[dict] = []
    for f in reversed(files):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for ln in reversed(lines):
            if q and q not in ln.lower():
                continue
            e = _parse(ln)
            if not e or (kind and e["kind"] != kind):
                continue
            out.append(e)
            if len(out) >= limit:
                return out
    return out


def clear() -> int:
    n = 0
    for f in _logs_dir().glob("access-*.log"):
        try:
            f.unlink()
            n += 1
        except Exception:
            pass
    return n


class _ErrorHandler(logging.Handler):
    """Routes any ERROR+ log record (uvicorn, libraries, our own) into the access
    log, so 'any Python error' is captured — not just unhandled request errors."""

    def emit(self, rec):
        try:
            if rec.name.startswith("waikiki.access"):
                return  # don't recurse on our own writes
            msg = rec.getMessage()
            if rec.exc_info:
                msg += " | " + "".join(
                    traceback.format_exception_only(*rec.exc_info[:2])).strip()
            record(f"ERROR {rec.name}", status=500, detail=msg)
        except Exception:
            pass


_installed = False


def setup() -> None:
    """Attach the ERROR capture handler to the root logger (idempotent)."""
    global _installed
    if _installed:
        return
    h = _ErrorHandler(level=logging.ERROR)
    logging.getLogger().addHandler(h)
    _installed = True
