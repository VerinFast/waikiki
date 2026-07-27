"""Run a local CLI and log the invocation to the debug log.

Every claude/agy/gemini call in Waikiki goes through here so the Help → CLI debug
view captures the command, exit code, timing, and full output. On timeout the
partial output is logged and the exception re-raised for the caller to handle.
"""
from __future__ import annotations

import subprocess
import time

from . import accesslog, debuglog, shellenv


def run(label: str, argv: list, timeout: int) -> subprocess.CompletedProcess:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=shellenv.env())
    except subprocess.TimeoutExpired as exc:
        dur = time.monotonic() - t0
        debuglog.record(label, argv, exc.stdout or "",
                        (exc.stderr or "") + f"\n[timed out after {timeout}s]", None, dur)
        accesslog.cli(label, None, dur)
        raise
    except Exception as exc:
        dur = time.monotonic() - t0
        debuglog.record(label, argv, "", f"[failed to launch: {exc}]", None, dur)
        accesslog.cli(label, "err", dur)
        raise
    dur = time.monotonic() - t0
    debuglog.record(label, argv, proc.stdout or "", proc.stderr or "",
                    proc.returncode, dur)
    accesslog.cli(label, proc.returncode, dur)
    return proc
