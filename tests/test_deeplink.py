"""Deep-link tests.

The refusal cases carry the weight here. A registered URL scheme is an
unauthenticated external input -- any web page can fire one at the app -- and the
desktop window is a loopback caller, which auth.py treats as owner. So anything
this module lets through is something the whole internet can ask the app to do.
"""
import pytest

from jamgraf import deeplink


# --- What must work -----------------------------------------------------------

def test_open_a_page():
    assert deeplink.resolve("jamgraf://beaconlight/meru") == \
        "/wiki/meru?wiki=beaconlight"


def test_open_a_page_at_a_section():
    assert deeplink.resolve("jamgraf://beaconlight/meru#abilities") == \
        "/wiki/meru?wiki=beaconlight#abilities"


def test_open_a_wiki_front_page():
    assert deeplink.resolve("jamgraf://beaconlight") == "/?wiki=beaconlight"


def test_trailing_slash_is_equivalent():
    assert deeplink.resolve("jamgraf://beaconlight/meru/") == \
        deeplink.resolve("jamgraf://beaconlight/meru")
    assert deeplink.resolve("jamgraf://beaconlight/") == \
        deeplink.resolve("jamgraf://beaconlight")


def test_bare_scheme_is_the_front_page():
    """jamgraf:// has an empty authority, which no wiki slug can be."""
    assert deeplink.resolve("jamgraf://") == "/"


def test_search_within_a_wiki():
    assert deeplink.resolve("jamgraf://beaconlight?q=clockwork") == \
        "/search?q=clockwork&wiki=beaconlight"


def test_search_is_always_wiki_scoped():
    """Wikis are isolated, so there is no unscoped search to express."""
    got = deeplink.resolve("jamgraf://beaconlight?q=clockwork")
    assert "wiki=beaconlight" in got


def test_search_terms_are_reencoded_not_echoed():
    """Multi-word and punctuated queries survive without injecting extra params."""
    got = deeplink.resolve("jamgraf://beaconlight?q=clockwork%20punk%26admin%3D1")
    assert got == "/search?q=clockwork%20punk%26admin%3D1&wiki=beaconlight"
    assert got.count("&") == 1          # only the one we added


def test_a_wiki_named_like_an_old_verb_is_reachable():
    """The reason there are no verbs: these must address wikis, not actions."""
    assert deeplink.resolve("jamgraf://search/notes") == "/wiki/notes?wiki=search"
    assert deeplink.resolve("jamgraf://home") == "/?wiki=home"
    assert deeplink.resolve("jamgraf://open/meru") == "/wiki/meru?wiki=open"


def test_scheme_is_case_insensitive():
    assert deeplink.resolve("JAMGRAF://beaconlight/meru") == \
        "/wiki/meru?wiki=beaconlight"


# --- What must be refused -----------------------------------------------------

@pytest.mark.parametrize("url", [
    "",
    None,
    "not a url",
    "http://evil.example.com/",             # wrong scheme entirely
    "https://jamgraf/a/b",
    "jamgraf:",                              # no authority at all
    "jamgraf://?q=x",                        # query with no wiki to scope it
    "jamgraf:///meru",                       # page with no wiki
    "jamgraf://beaconlight/meru/extra",      # more than one page segment
])
def test_refuses_malformed_or_unknown(url):
    assert deeplink.resolve(url) is None


@pytest.mark.parametrize("url", [
    "jamgraf://beaconlight/../../etc/passwd",
    "jamgraf://beaconlight/../../settings",
    "jamgraf://beaconlight/..%2f..%2fsettings",
    "jamgraf://beaconlight/meru/extra/segments",
])
def test_refuses_path_traversal(url):
    """A deep link must never be able to address a path we didn't construct."""
    assert deeplink.resolve(url) is None


def test_a_page_named_settings_is_a_page_not_the_settings_route():
    """`/wiki/settings` is a wiki page; `/settings` is the owner-only app route.

    A wiki is allowed to contain a page called "settings" -- resolving it must
    address the page view and drop any caller-supplied query.
    """
    got = deeplink.resolve("jamgraf://beaconlight/settings?x=1")
    assert got == "/wiki/settings?wiki=beaconlight"
    assert "x=1" not in got


@pytest.mark.parametrize("url", [
    "jamgraf://BEACONLIGHT/Meru",          # slugs are lowercase
    "jamgraf://beacon light/meru",         # space
    "jamgraf://beaconlight/me%2Fru",       # encoded slash
    "jamgraf://-leading-dash/meru",
    "jamgraf://beaconlight/meru#Bad Anchor",
    "jamgraf://beaconlight/meru#../../x",
])
def test_refuses_slugs_and_anchors_that_are_not_slugs(url):
    assert deeplink.resolve(url) is None


def test_refuses_an_overlong_slug():
    assert deeplink.resolve(f"jamgraf://{'a' * 200}/meru") is None


@pytest.mark.parametrize("url", [
    "jamgraf://beaconlight?q=",
    "jamgraf://beaconlight?q=%20",              # whitespace only
    "jamgraf://beaconlight?wiki=other",         # a query but no terms
])
def test_refuses_bad_searches(url):
    assert deeplink.resolve(url) is None


def test_a_caller_supplied_wiki_param_cannot_redirect_the_scope():
    """The scope comes from the authority we validated, never from the query."""
    got = deeplink.resolve("jamgraf://beaconlight?q=x&wiki=../../etc")
    assert got == "/search?q=x&wiki=beaconlight"


def test_refuses_an_overlong_query():
    assert deeplink.resolve(f"jamgraf://beaconlight?q={'x' * 300}") is None


def test_settings_is_not_reachable():
    """The whole point of the allow-list: owner-only surfaces stay unreachable."""
    for url in ("jamgraf://main/settings/updates/install",
                "jamgraf://main/../settings",
                "jamgraf://main/settings/edit"):
        got = deeplink.resolve(url)
        # Never the app's /settings tree. A page path (/wiki/settings) is fine.
        assert got is None or not got.startswith("/settings")


def test_no_resolution_ever_contains_a_host():
    """Paths are app-relative; the base is added by the caller, never by input."""
    for url in ("jamgraf://beaconlight/meru", "jamgraf://",
                "jamgraf://beaconlight", "jamgraf://beaconlight?q=x"):
        got = deeplink.resolve(url)
        assert got and got.startswith("/") and not got.startswith("//")


# --- Round trip ---------------------------------------------------------------

def test_for_page_round_trips():
    link = deeplink.for_page("beaconlight", "meru")
    assert deeplink.resolve(link) == "/wiki/meru?wiki=beaconlight"


def test_for_page_with_section_round_trips():
    link = deeplink.for_page("beaconlight", "meru", section="abilities")
    assert deeplink.resolve(link) == "/wiki/meru?wiki=beaconlight#abilities"


def test_for_wiki_round_trips():
    assert deeplink.resolve(deeplink.for_wiki("beaconlight")) == "/?wiki=beaconlight"


# --- Exposure to callers ------------------------------------------------------

def test_mcp_get_page_hands_out_a_deep_link(wiki, monkeypatch):
    """The agent case this exists for: a link a human can actually open.

    An http:// URL goes stale when the app picks a different port, so get_page
    reports the jamgraf:// form instead.
    """
    from jamgraf import mcp_server, store

    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    monkeypatch.setattr(mcp_server.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    store.create_page("Meru", "# Meru\n\nspirit")
    res = mcp_server.get_page("meru")
    assert res["link"] == "jamgraf://main/meru"
    assert deeplink.resolve(res["link"]) == "/wiki/meru?wiki=main"


def test_page_view_offers_the_deep_link(wiki):
    """The Copy link button carries the scheme URL, not the http one."""
    from fastapi.testclient import TestClient

    from jamgraf import store
    from jamgraf.api import app

    store.create_page("Meru", "# Meru\n\nspirit")
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        body = c.get("/wiki/meru").text
    assert "jamgraf://main/meru" in body and "wkCopyLink" in body
