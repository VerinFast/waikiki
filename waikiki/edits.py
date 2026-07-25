"""Pure text-edit planners.

Each planner takes the current text and returns a `(start, end, insert)` splice
(replace text[start:end] with `insert`) — or raises ValueError with an
actionable message. Both the live CRDT path (collab.apply_edit → surgical
delete+insert) and the headless DB fallback (string splice) use these, so their
behavior is identical.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _require_unique(text: str, needle: str) -> int:
    if not needle:
        raise ValueError("empty anchor text")
    idx = text.find(needle)
    if idx == -1:
        raise ValueError("text not found in the page")
    if text.find(needle, idx + len(needle)) != -1:
        raise ValueError("text is not unique — include more surrounding context")
    return idx


def plan_edit(text: str, old: str, new: str) -> tuple[int, int, str]:
    idx = _require_unique(text, old)
    return (idx, idx + len(old), new)


def plan_remove(text: str, snippet: str) -> tuple[int, int, str]:
    idx = _require_unique(text, snippet)
    return (idx, idx + len(snippet), "")


def plan_prepend(text: str, new: str) -> tuple[int, int, str]:
    return (0, 0, new if new.endswith("\n") else new + "\n")


def plan_insert(text: str, new: str, *, after: str | None = None,
                before: str | None = None, position: int | None = None
                ) -> tuple[int, int, str]:
    if position is not None:
        pos = max(0, min(int(position), len(text)))
    elif after is not None:
        pos = _require_unique(text, after) + len(after)
    elif before is not None:
        pos = _require_unique(text, before)
    else:
        pos = len(text)  # plain append
    return (pos, pos, new)


def section_span(text: str, heading: str) -> tuple[int, int] | None:
    """Char range covering a section: from its heading line through just before
    the next heading of the same-or-higher level (or end of doc). None if the
    heading isn't found exactly once."""
    lines = text.split("\n")
    hits = [(i, len(m.group(1))) for i, ln in enumerate(lines)
            if (m := _HEADING.match(ln)) and m.group(2).strip() == heading.strip()]
    if len(hits) != 1:
        return None
    start_line, level = hits[0]
    end_line = len(lines)
    for j in range(start_line + 1, len(lines)):
        m = _HEADING.match(lines[j])
        if m and len(m.group(1)) <= level:
            end_line = j
            break
    start = sum(len(lines[k]) + 1 for k in range(start_line))
    end = min(sum(len(lines[k]) + 1 for k in range(end_line)), len(text))
    return (start, end)


def plan_replace_section(text: str, heading: str, new_markdown: str
                         ) -> tuple[int, int, str]:
    span = section_span(text, heading)
    if span is None:
        raise ValueError("heading not found, or not unique, in the page")
    start, end = span
    body = new_markdown.rstrip("\n") + "\n"
    if end < len(text):
        body += "\n"  # keep a blank line before the following section
    return (start, end, body)


def make_planner(b: dict):
    """Build a planner from a JSON op description (op + args). None if unknown."""
    op = b.get("op")
    if op == "edit":
        return lambda s: plan_edit(s, b["old"], b["new"])
    if op == "remove":
        return lambda s: plan_remove(s, b["text"])
    if op == "prepend":
        return lambda s: plan_prepend(s, b["text"])
    if op == "insert":
        return lambda s: plan_insert(s, b["text"], after=b.get("after"),
                                     before=b.get("before"), position=b.get("position"))
    if op == "replace_section":
        return lambda s: plan_replace_section(s, b["heading"], b["markdown"])
    return None


def apply_to_string(text: str, planner) -> str:
    """Apply a planner to a plain string (headless fallback)."""
    start, end, insert = planner(text)
    return text[:start] + insert + text[end:]
