"""Frontmatter + structured data helpers (Sprint C).

A page may start with a YAML-lite frontmatter block:

    ---
    tags: character, spirit
    home: Beaconlight
    hp: 42
    ---
    # Body...

`tags` becomes the page's tags (for auto-index pages); the rest become typed
properties rendered as an infobox.
"""
from __future__ import annotations

import re

_FM = re.compile(r"^\s*---[ \t]*\n(.*?)\n---[ \t]*\n?", re.S)


def parse_frontmatter(markdown: str) -> tuple[dict, list[str], str]:
    """Return (properties, tags, body_without_frontmatter).

    Line endings are normalized to LF first: the web editor's form submit sends
    Windows CRLF, and the fence regex (``---[ \\t]*\\n``) won't match ``---\\r\\n``,
    which would silently drop the whole frontmatter block (and the page's tags)."""
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    m = _FM.match(text)
    if not m:
        return {}, [], text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            k = k.strip()
            if k:
                meta[k] = v.strip()
    tags: list[str] = []
    for key in list(meta):
        if key.lower() == "tags":
            tags = [t.strip().lower() for t in re.split(r"[,;]", meta.pop(key)) if t.strip()]
    return meta, tags, text[m.end():]
