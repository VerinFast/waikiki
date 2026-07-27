"""App-global config (not per-wiki), stored in DATA_DIR/app_config.json.

Per-wiki preferences live in each wiki's `settings` table; things that are truly
global to the install — like the access-log retention — live here so they don't
have to be duplicated per wiki.
"""
from __future__ import annotations

import json
import threading

from . import config

_lock = threading.Lock()


def _path():
    return config.DATA_DIR / "app_config.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def get(key: str, default=None):
    return _load().get(key, default)


def set(key: str, value) -> None:
    with _lock:
        data = _load()
        data[key] = value
        _path().write_text(json.dumps(data, indent=2))
