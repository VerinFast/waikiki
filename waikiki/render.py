"""Markdown -> HTML rendering with GFM tables, fenced code, and [[wiki links]]."""
from __future__ import annotations

import re

from markdown_it import MarkdownIt

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


# "gfm-like" enables tables, strikethrough, and linkify out of the box.
_md = MarkdownIt("gfm-like", {"highlight": _highlight, "linkify": True, "html": False})

# [[Wiki Link]] and [[slug|Label]] -> internal links.
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")


def _expand_wikilinks(markdown: str) -> str:
    def repl(m: re.Match) -> str:
        target, label = m.group(1).strip(), m.group(2)
        label = (label or target).strip()
        return f"[{label}](/wiki/{_slugify(target)})"

    return _WIKILINK.sub(repl, markdown)


def render_markdown(markdown: str) -> str:
    """Return sanitized HTML. `html=False` means raw HTML in source is escaped."""
    return _md.render(_expand_wikilinks(markdown or ""))


def pygments_css() -> str:
    return HtmlFormatter().get_style_defs(".highlight") if _HAS_PYGMENTS else ""


slugify = _slugify
