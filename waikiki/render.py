"""Markdown -> HTML rendering with GFM tables, fenced code, and [[wiki links]]."""
from __future__ import annotations

import re

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name, guess_lexer

    _HAS_PYGMENTS = True
except Exception:  # pragma: no cover
    _HAS_PYGMENTS = False


def _highlight(code: str, lang: str, _attrs) -> str:
    if _HAS_PYGMENTS:
        try:
            lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
            return highlight(code, lexer, HtmlFormatter(nowrap=False))
        except Exception:
            pass
    # markdown-it escapes for us when we return "" — but we've taken over, so escape.
    escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre><code>{escaped}</code></pre>"


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")


# "gfm-like" enables tables, strikethrough, and linkify; anchors_plugin adds
# `id`s + clickable "#" permalinks to headings so sections are deep-linkable.
_md = MarkdownIt("gfm-like", {"highlight": _highlight, "linkify": True, "html": False})
_md.use(anchors_plugin, min_level=1, max_level=3, slug_func=_slugify,
        permalink=True, permalinkSymbol="#")

# [[Wiki Link]], [[slug|Label]], [[Page#Section]], [[#Section]] -> internal links.
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _wikilink_href(target: str) -> str:
    """Resolve a wikilink target (optionally 'Page#Section' or '#Section')."""
    page_part, _, section = target.partition("#")
    if page_part.strip():
        href = "/wiki/" + _slugify(page_part)
        if section.strip():
            href += "#" + _slugify(section)
        return href
    return "#" + _slugify(section)  # same-page section link


def _expand_wikilinks(markdown: str) -> str:
    def repl(m: re.Match) -> str:
        target, label = m.group(1).strip(), m.group(2)
        label = (label or target).strip()
        return f"[{label}]({_wikilink_href(target)})"

    return _WIKILINK.sub(repl, markdown)


def extract_wikilinks(markdown: str) -> list[str]:
    """Return the target *page* slugs of cross-page [[wiki links]] (ignores the
    section part and same-page #links) — for the resolved/broken link stats."""
    out = []
    for m in _WIKILINK.finditer(markdown or ""):
        page_part = m.group(1).split("#", 1)[0].strip()
        if page_part:
            out.append(_slugify(page_part))
    return out


_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


def extract_toc(markdown: str) -> list[dict]:
    """Extract level-1..3 headings for an on-page table of contents."""
    toc, in_fence = [], False
    for line in (markdown or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if m:
            text = m.group(2).strip()
            toc.append({"level": len(m.group(1)), "text": text, "slug": _slugify(text)})
    return toc


def render_markdown(markdown: str) -> str:
    """Return sanitized HTML. `html=False` means raw HTML in source is escaped."""
    return _md.render(_expand_wikilinks(markdown or ""))


def pygments_css() -> str:
    return HtmlFormatter().get_style_defs(".highlight") if _HAS_PYGMENTS else ""


slugify = _slugify
