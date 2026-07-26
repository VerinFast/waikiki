"""A small on-disk log of every local-CLI invocation (claude / agy / gemini).

Both the web app and the (separate-process) MCP server append to the same JSONL
file under the data dir, so the Help → CLI debug view shows what actually ran —
the command, exit code, duration, and full stdout/stderr — regardless of which
process spawned it. This is the place to look when image generation or chat
"does nothing".
"""
from __future__ import annotations

import datetime
import json
import threading

from . import config

_lock = threading.Lock()
_MAX_IO = 16000      # truncate captured stdout/stderr per entry
_MAX_ARG = 4000      # truncate each argv element (prompts can be huge)
_MAX_BYTES = 2_000_000
_KEEP = 200          # entries retained on rotation


def _path():
    return config.DATA_DIR / "cli_debug.jsonl"


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s) - n} chars]"


def record(label: str, argv, stdout: str, stderr: str, returncode,
           duration: float, wiki: str | None = None) -> None:
    try:
        if wiki is None:
            from . import db
            try:
                wiki = db.active_wiki()
            except Exception:
                wiki = None
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "wiki": wiki,
            "argv": [_trunc(str(a), _MAX_ARG) for a in argv],
            "returncode": returncode,
            "duration": round(duration, 2),
            "stdout": _trunc(stdout, _MAX_IO),
            "stderr": _trunc(stderr, _MAX_IO),
        }
        line = json.dumps(entry, ensure_ascii=False)
        path = _path()
        with _lock:
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass  # logging must never break the actual work


def _rotate(path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-_KEEP:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def tail(n: int = 100) -> list[dict]:
    """Most-recent-first list of the last n entries."""
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    out.reverse()
    return out


def clear() -> None:
    with _lock:
        try:
            _path().write_text("", encoding="utf-8")
        except Exception:
            pass
