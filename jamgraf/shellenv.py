"""Resolve the user's real shell PATH for spawning local CLIs.

A macOS .app launched from Finder inherits a minimal PATH, so CLIs installed via
homebrew, npm-global, nvm, volta, etc. aren't found by `shutil.which` even though
they work in a terminal. We recover the login shell's PATH once and reuse it
wherever we look up or spawn a CLI (claude, gemini, agy).
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess

# Common install locations, added even if the login-shell probe fails.
_EXTRA = [
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin",
    "~/.local/bin", "~/bin", "~/.claude/local", "~/.npm-global/bin", "~/.bun/bin",
    "~/.deno/bin", "~/.volta/bin", "~/.cargo/bin",
]


@functools.lru_cache(maxsize=1)
def augmented_path() -> str:
    parts: list[str] = []
    shell = os.environ.get("SHELL", "/bin/zsh")
    try:
        out = subprocess.run([shell, "-lic", "echo $PATH"],
                             capture_output=True, text=True, timeout=6)
        if out.returncode == 0 and out.stdout.strip():
            parts += out.stdout.strip().split(":")
    except Exception:
        pass
    parts += os.environ.get("PATH", "").split(":")
    parts += [os.path.expanduser(p) for p in _EXTRA]
    seen: set[str] = set()
    ordered = [p for p in parts if p and not (p in seen or seen.add(p))]
    return ":".join(ordered)


def which(cmd: str) -> str | None:
    """Resolve a CLI, honoring absolute paths and the augmented PATH."""
    if not cmd:
        return None
    if os.path.isabs(cmd):
        return cmd if os.access(cmd, os.X_OK) else None
    return shutil.which(cmd, path=augmented_path())


def env() -> dict:
    """os.environ with PATH augmented, for spawning CLIs (and their children)."""
    e = dict(os.environ)
    e["PATH"] = augmented_path()
    return e
