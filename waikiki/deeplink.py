"""``waikiki://`` deep links — external URL to in-app destination.

Lets anything outside the app hand out a stable link to a page. The wiki is the
authority, mirroring how a wiki is the outermost unit of isolation everywhere
else in the app:

    waikiki://beaconlight/meru
    waikiki://beaconlight/meru#abilities
    waikiki://beaconlight                (the wiki's front page)
    waikiki://beaconlight?q=clockwork    (search, inside that wiki)
    waikiki://                           (the front page)

A stable link matters because the HTTP URL isn't stable: ``waikiki_app`` scans
for a free port (``_pick_port``), so today's ``127.0.0.1:8787`` can be 8788
tomorrow. The scheme doesn't care.

There are deliberately **no verbs**. Putting the wiki in the authority position
means a reserved word like ``search`` or ``home`` would shadow any wiki actually
named that, so search is a query on a wiki instead, and the bare ``waikiki://``
(empty authority — a slug can never be empty) is the front page. Search being
wiki-scoped also matches the isolation rule: a search never spans wikis, so
there is no such thing as an unscoped one.

Why this is an allow-list and not a path passthrough
----------------------------------------------------
A registered URL scheme is an **unauthenticated external input**: any web page,
mail message, or other app can fire ``waikiki://...`` at us, and macOS will
deliver it. The desktop window loads over loopback, and ``auth.py`` grants
loopback callers **owner** rights -- so a design that forwarded the URL's path
straight to ``load_url`` would let any website drive owner-level routes in
someone's wiki (open Settings, hit an export, trip anything that mutates on GET).

So this module translates a small, fixed set of shapes into paths it constructs
itself, and refuses everything else. It never echoes a caller-supplied path, and
never a caller-supplied host -- the host is always our own loopback base, added
by the caller of ``resolve()``. Widening what this accepts widens what the whole
internet can ask this app to do; treat it that way.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, unquote, urlparse

SCHEME = "waikiki"

# Slugs as `wikis.slugify` / `store` produce them. Deliberately strict: no dots,
# no slashes, no percent escapes -- so nothing here can walk a path or smuggle a
# second URL component through.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

# Heading anchors come from render.py's slug_func, same shape as a page slug but
# allowed to contain digits-only forms (e.g. "#2-history").
_FRAGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

MAX_QUERY = 256


def _ok_slug(value: str) -> bool:
    return bool(value) and bool(_SLUG.match(value))


def resolve(url: str) -> str | None:
    """Translate a ``waikiki://`` URL into an app-relative path.

    Returns a path like ``/wiki/meru?wiki=beaconlight#abilities``, or **None**
    for anything not explicitly allowed. Never raises -- a malformed link from
    the outside world is a refusal, not an error.
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    try:
        parts = urlparse(text)
    except Exception:
        return None
    if (parts.scheme or "").lower() != SCHEME:
        return None
    # Require the authority form. The wiki lives in the authority, so "waikiki:"
    # with no "//" is malformed rather than a shorthand -- keep the shape exact.
    if not text[len(parts.scheme):].startswith("://"):
        return None

    wiki = parts.netloc or ""
    # urlparse keeps the leading "/" on the path; dropping empty segments makes
    # waikiki://w/p and waikiki://w/p/ equivalent.
    segments = [s for s in (parts.path or "").split("/") if s]

    if not wiki:
        # waikiki:// -- the front page. No slug is empty, so this can't collide
        # with a wiki. Anything hung off it is malformed.
        return "/" if not segments and not parts.query else None

    if not _ok_slug(wiki):
        return None

    if segments:
        return _resolve_page(wiki, segments, parts.fragment)

    if parts.query:
        return _resolve_search(wiki, parts.query)

    return f"/?wiki={quote(wiki)}"       # the wiki's front page


def _resolve_page(wiki: str, segments: list[str], fragment: str) -> str | None:
    """waikiki://<wiki>/<page>[#section]"""
    if len(segments) != 1:              # exactly one page segment, never a path
        return None
    page = segments[0]
    if not _ok_slug(page):
        return None
    # A page link ignores any query: the destination is fully determined by the
    # wiki and slug, and echoing caller params would widen the surface.
    path = f"/wiki/{quote(page)}?wiki={quote(wiki)}"

    if fragment:
        frag = unquote(fragment).lower()
        if not _FRAGMENT.match(frag):
            return None                 # a bad anchor refuses the whole link
        path += f"#{quote(frag)}"
    return path


def _resolve_search(wiki: str, query: str) -> str | None:
    """waikiki://<wiki>?q=<terms> -- search is always scoped to one wiki."""
    if len(query) > MAX_QUERY:
        return None
    try:
        params = parse_qs(query, keep_blank_values=False)
    except Exception:
        return None
    terms = (params.get("q") or [""])[0].strip()
    if not terms:
        return None
    return f"/search?q={quote(terms)}&wiki={quote(wiki)}"


# --- Link construction --------------------------------------------------------

def for_page(wiki: str, page: str, section: str | None = None) -> str:
    """The ``waikiki://`` link that opens a page. Inverse of ``resolve``."""
    link = f"{SCHEME}://{quote(wiki)}/{quote(page)}"
    if section:
        link += f"#{quote(section)}"
    return link


def for_wiki(wiki: str) -> str:
    return f"{SCHEME}://{quote(wiki)}"


def for_search(wiki: str, terms: str) -> str:
    return f"{SCHEME}://{quote(wiki)}?q={quote(terms)}"
