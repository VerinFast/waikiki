"""``waikiki://`` deep links — external URL to in-app destination.

Lets anything outside the app hand out a stable link to a page:

    waikiki://open/beaconlight/meru
    waikiki://open/beaconlight/meru#abilities
    waikiki://open/beaconlight                 (the wiki's front page)
    waikiki://search?q=clockwork&wiki=beaconlight
    waikiki://home

A stable link matters because the HTTP URL isn't stable: ``waikiki_app`` scans
for a free port (``_pick_port``), so today's ``127.0.0.1:8787`` can be 8788
tomorrow. The scheme doesn't care.

Why this is an allow-list and not a path passthrough
----------------------------------------------------
A registered URL scheme is an **unauthenticated external input**: any web page,
mail message, or other app can fire ``waikiki://...`` at us, and macOS will
deliver it. The desktop window loads over loopback, and ``auth.py`` grants
loopback callers **owner** rights -- so a design that forwarded the URL's path
straight to ``load_url`` would let any website drive owner-level routes in
someone's wiki (open Settings, hit an export, trip anything that mutates on GET).

So this module translates a small, fixed set of verbs into paths it constructs
itself, and refuses everything else. It never echoes a caller-supplied path, and
never a caller-supplied host -- the host is always our own loopback base, added
by the caller of ``resolve()``. Adding a verb here widens what the whole internet
can ask this app to do; treat it that way.
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
    try:
        parts = urlparse(url.strip())
    except Exception:
        return None
    if (parts.scheme or "").lower() != SCHEME:
        return None

    action = (parts.netloc or "").lower()
    # urlparse keeps the leading "/" on the path; drop empty segments so both
    # waikiki://open/a/b and waikiki://open/a/b/ behave the same.
    segments = [s for s in (parts.path or "").split("/") if s]

    if action == "home":
        return "/" if not segments else None

    if action == "open":
        return _resolve_open(segments, parts.fragment)

    if action == "search":
        return _resolve_search(parts.query)

    return None                     # unknown verb -- refuse


def _resolve_open(segments: list[str], fragment: str) -> str | None:
    """waikiki://open/<wiki>[/<page>][#section]"""
    if not segments or len(segments) > 2:
        return None
    wiki = segments[0]
    if not _ok_slug(wiki):
        return None

    if len(segments) == 1:          # a wiki, no page: its front page
        return f"/?wiki={quote(wiki)}"

    page = segments[1]
    if not _ok_slug(page):
        return None
    path = f"/wiki/{quote(page)}?wiki={quote(wiki)}"

    if fragment:
        frag = unquote(fragment).lower()
        if not _FRAGMENT.match(frag):
            return None             # a bad anchor refuses the whole link
        path += f"#{quote(frag)}"
    return path


def _resolve_search(query: str) -> str | None:
    """waikiki://search?q=<terms>[&wiki=<wiki>]"""
    if not query or len(query) > MAX_QUERY:
        return None
    try:
        params = parse_qs(query, keep_blank_values=False)
    except Exception:
        return None
    terms = (params.get("q") or [""])[0].strip()
    if not terms:
        return None
    path = f"/search?q={quote(terms)}"
    wiki = (params.get("wiki") or [""])[0].strip().lower()
    if wiki:
        if not _ok_slug(wiki):
            return None
        path += f"&wiki={quote(wiki)}"
    return path


# --- Link construction --------------------------------------------------------

def for_page(wiki: str, page: str, section: str | None = None) -> str:
    """The ``waikiki://`` link that opens a page. Inverse of ``resolve``."""
    link = f"{SCHEME}://open/{quote(wiki)}/{quote(page)}"
    if section:
        link += f"#{quote(section)}"
    return link


def for_wiki(wiki: str) -> str:
    return f"{SCHEME}://open/{quote(wiki)}"
