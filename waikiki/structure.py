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


# The frontmatter keys that are *structure*, not properties: `title` is the
# page's title column and `parent` is its place in the hierarchy. Like `tags`
# (and `store.TEMPLATE_KEY`) they live in the same block but are their own
# concept — the markdown export writes them so a folder of `.md` can be read
# back into the same pages, and the import takes them out again rather than
# leaving two rows in every infobox naming what the page already is.
STRUCTURAL_KEYS = ("title", "parent")


def build_frontmatter(meta: dict, tags: list[str] | None = None) -> str:
    """Render a frontmatter block from properties + tags ("" when both empty).

    Property order is the caller's; `tags` leads the block, the way every other
    writer in the app emits one (`store.set_properties` and friends)."""
    lines = [f"{k}: {v}" for k, v in (meta or {}).items()]
    if tags:
        lines.insert(0, "tags: " + ", ".join(tags))
    return ("---\n" + "\n".join(lines) + "\n---\n") if lines else ""


def take_structural(markdown: str) -> tuple[dict, str]:
    """Split the structural keys out of a page's frontmatter.

    Returns ``({"title": ..., "parent": ...}, markdown_without_them)`` — keys
    matched case-insensitively, absent ones simply missing. Text carrying none
    of them comes back **byte-identical**: a hand-written file that Waikiki did
    not export is not ours to reformat."""
    meta, tags, body = parse_frontmatter(markdown)
    found: dict[str, str] = {}
    for key in list(meta):
        if key.lower() in STRUCTURAL_KEYS:
            found[key.lower()] = meta.pop(key).strip()
    if not found:
        return {}, markdown
    return found, build_frontmatter(meta, tags) + body.lstrip("\n")
