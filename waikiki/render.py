"""Markdown -> HTML rendering with GFM tables, fenced code, and [[wiki links]]."""
from __future__ import annotations

import re
from html import escape as _esc

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin


def _timeline_html(code: str) -> str:
    """A ```timeline fenced block: one 'When: What' per line -> a timeline."""
    items = []
    for line in code.splitlines():
        line = line.strip()
        if not line:
            continue
        when, sep, what = line.partition(":")
        if not sep:
            when, what = "", when
        items.append(f'<div class="tl-item"><div class="tl-when">{_esc(when.strip())}'
                     f'</div><div class="tl-what">{_esc(what.strip())}</div></div>')
    return '<div class="timeline">' + "".join(items) + "</div>"


def infobox(meta: dict) -> str:
    """Render frontmatter properties as an infobox (structured-data table)."""
    if not meta:
        return ""
    rows = "".join(f"<tr><th>{_esc(str(k))}</th><td>{_esc(str(v))}</td></tr>"
                   for k, v in meta.items())
    return f'<table class="infobox">{rows}</table>'

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name, guess_lexer

    _HAS_PYGMENTS = True
except Exception:  # pragma: no cover
    _HAS_PYGMENTS = False


def _highlight(code: str, lang: str, _attrs) -> str:
    if lang == "timeline":            # component: ```timeline fenced block
        return _timeline_html(code)
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


def _valid_link(url: str) -> bool:
    u = (url or "").strip().lower()
    return not (u.startswith("javascript:") or u.startswith("vbscript:"))


def _make_md(allow_html: bool) -> "MarkdownIt":
    md = MarkdownIt("gfm-like",
                    {"highlight": _highlight, "linkify": True, "html": allow_html})
    md.use(anchors_plugin, min_level=1, max_level=3, slug_func=_slugify,
           permalink=True, permalinkSymbol="#")
    md.validateLink = _valid_link  # allow file:/mailto:/data: (block js:) on a local wiki
    return md


_md = _make_md(False)          # default: raw HTML escaped
_md_html = _make_md(True)      # opt-in per wiki: raw HTML passes through

# Post-render tweaks (simpler + more robust than renderer-rule overrides):
_VIDEO = ("mp4", "webm", "mov", "ogv")
_AUDIO = ("mp3", "wav", "ogg", "m4a")
_IMG_MEDIA = re.compile(
    r'<img\b[^>]*\bsrc="([^"]+\.(?:mp4|webm|mov|ogv|mp3|wav|ogg|m4a))"[^>]*>', re.I)
_EXT_LINK = re.compile(r'<a href="(https?://[^"]+)"')
# the ```timeline component returns raw HTML that markdown-it wraps in <pre><code>.
_TL_WRAP = re.compile(
    r'<pre><code class="language-timeline">(.*?)</code></pre>', re.S)


def _post_process(html: str) -> str:
    html = _TL_WRAP.sub(lambda m: m.group(1), html)   # unwrap timeline component

    def media(m: re.Match) -> str:
        src = m.group(1)
        kind = "video" if src.rsplit(".", 1)[-1].lower() in _VIDEO else "audio"
        return f'<{kind} src="{src}" controls style="max-width:100%"></{kind}>'

    html = _IMG_MEDIA.sub(media, html)
    html = _EXT_LINK.sub(
        r'<a class="ext" target="_blank" rel="noopener noreferrer" href="\1"', html)
    return html

# [[Wiki Link]], [[slug|Label]], [[Page#Section]], [[#Section]] -> internal links.
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _wikilink_href(target: str, resolver=None) -> str:
    """Resolve a wikilink target (optionally 'Page#Section' or '#Section').

    `resolver(slugified_page)` -> canonical slug maps a page *title or slug* to the
    real page slug (link-by-title), so links survive renames. Falls back to the
    slugified target (a red link) when unresolved."""
    page_part, _, section = target.partition("#")
    if page_part.strip():
        key = _slugify(page_part)
        slug = (resolver(key) if resolver else None) or key
        href = "/wiki/" + slug
        if section.strip():
            href += "#" + _slugify(section)
        return href
    return "#" + _slugify(section)  # same-page section link


def _expand_wikilinks(markdown: str, resolver=None) -> str:
    def repl(m: re.Match) -> str:
        target, label = m.group(1).strip(), m.group(2)
        label = (label or target).strip()
        return f"[{label}]({_wikilink_href(target, resolver)})"

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
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPH = re.compile(r"[*_`~]")


def _heading_display(raw: str) -> str:
    """The heading's rendered text: [[Page|Label]]->Label, [t](u)->t, no emphasis
    — so its slug matches the id the anchor plugin assigns."""
    t = _WIKILINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), raw)
    t = _MD_LINK.sub(r"\1", t)
    t = _EMPH.sub("", t)
    return t.strip()


def _headings(markdown: str) -> list[tuple[int, int, str, str]]:
    """(line_index, level, display, slug) for each h1-3, with the SAME slug the
    anchor plugin uses (deduped: notes, notes-1, notes-2, …)."""
    out, in_fence, seen = [], False, {}
    for i, line in enumerate((markdown or "").split("\n")):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        display = _heading_display(m.group(2).strip())
        base = _slugify(display)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append((i, len(m.group(1)), display, base if n == 0 else f"{base}-{n}"))
    return out


def extract_toc(markdown: str) -> list[dict]:
    """Level-1..3 headings for the table of contents (slug matches the anchor id)."""
    return [{"level": lvl, "text": disp, "slug": slug}
            for (_i, lvl, disp, slug) in _headings(markdown)]


def section_span_for_slug(markdown: str, slug: str) -> tuple[int, int] | None:
    """Char range of the section whose heading anchor is `slug`."""
    lines = (markdown or "").split("\n")
    heads = _headings(markdown)
    target = next((h for h in heads if h[3] == slug), None)
    if not target:
        return None
    start_line, level = target[0], target[1]
    end_line = len(lines)
    for h in heads:
        if h[0] > start_line and h[1] <= level:
            end_line = h[0]
            break
    start = sum(len(lines[k]) + 1 for k in range(start_line))
    end = min(sum(len(lines[k]) + 1 for k in range(end_line)), len(markdown))
    return (start, end)


def render_markdown(markdown: str, resolver=None, allow_html: bool = False) -> str:
    """Render markdown to HTML. `allow_html` (per-wiki opt-in) passes raw HTML
    through; otherwise raw HTML is escaped."""
    md = _md_html if allow_html else _md
    return _post_process(md.render(_expand_wikilinks(markdown or "", resolver)))


def pygments_css() -> str:
    return HtmlFormatter().get_style_defs(".highlight") if _HAS_PYGMENTS else ""


slugify = _slugify
