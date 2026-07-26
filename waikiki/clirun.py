"""Run a local CLI and log the invocation to the debug log.

Every claude/agy/gemini call in Waikiki goes through here so the Help → CLI debug
view captures the command, exit code, timing, and full output. On timeout the
partial output is logged and the exception re-raised for the caller to handle.
"""
from __future__ import annotations

import subprocess
import time

from . import debuglog, shellenv


def run(label: str, argv: list, timeout: int) -> subprocess.CompletedProcess:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=shellenv.env())
    except subprocess.TimeoutExpired as exc:
        debuglog.record(label, argv, exc.stdout or "",
                        (exc.stderr or "") + f"\n[timed out after {timeout}s]",
                        None, time.monotonic() - t0)
        raise
    except Exception as exc:
        debuglog.record(label, argv, "", f"[failed to launch: {exc}]", None,
                        time.monotonic() - t0)
        raise
    debuglog.record(label, argv, proc.stdout or "", proc.stderr or "",
                    proc.returncode, time.monotonic() - t0)
    return proc
