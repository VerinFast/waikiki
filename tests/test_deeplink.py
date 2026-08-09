"""Deep-link tests.

The refusal cases carry the weight here. A registered URL scheme is an
unauthenticated external input -- any web page can fire one at the app -- and the
desktop window is a loopback caller, which auth.py treats as owner. So anything
this module lets through is something the whole internet can ask the app to do.
"""
import pytest

from waikiki import deeplink


# --- What must work -----------------------------------------------------------

def test_open_a_page():
    assert deeplink.resolve("waikiki://open/beaconlight/meru") == \
        "/wiki/meru?wiki=beaconlight"


def test_open_a_page_at_a_section():
    assert deeplink.resolve("waikiki://open/beaconlight/meru#abilities") == \
        "/wiki/meru?wiki=beaconlight#abilities"


def test_open_a_wiki_front_page():
    assert deeplink.resolve("waikiki://open/beaconlight") == "/?wiki=beaconlight"


def test_trailing_slash_is_equivalent():
    assert deeplink.resolve("waikiki://open/beaconlight/meru/") == \
        deeplink.resolve("waikiki://open/beaconlight/meru")


def test_home():
    assert deeplink.resolve("waikiki://home") == "/"


def test_search():
    assert deeplink.resolve("waikiki://search?q=clockwork") == "/search?q=clockwork"


def test_search_scoped_to_a_wiki():
    got = deeplink.resolve("waikiki://search?q=clockwork&wiki=beaconlight")
    assert got == "/search?q=clockwork&wiki=beaconlight"


def test_search_terms_are_reencoded_not_echoed():
    """Multi-word and punctuated queries survive without injecting extra params."""
    got = deeplink.resolve("waikiki://search?q=clockwork%20punk%26admin%3D1")
    assert got == "/search?q=clockwork%20punk%26admin%3D1"
    assert got.count("&") == 0      # the & stayed inside the value


def test_scheme_is_case_insensitive():
    assert deeplink.resolve("WAIKIKI://open/beaconlight/meru") == \
        "/wiki/meru?wiki=beaconlight"


# --- What must be refused -----------------------------------------------------

@pytest.mark.parametrize("url", [
    "",
    None,
    "not a url",
    "http://evil.example.com/",             # wrong scheme entirely
    "https://waikiki/open/a/b",
    "waikiki://",                            # no verb
    "waikiki://open",                        # verb with no target
    "waikiki://nope/beaconlight/meru",       # unknown verb
    "waikiki://home/extra",                  # home takes no path
])
def test_refuses_malformed_or_unknown(url):
    assert deeplink.resolve(url) is None


@pytest.mark.parametrize("url", [
    "waikiki://open/../../etc/passwd",
    "waikiki://open/beaconlight/../../settings",
    "waikiki://open/beaconlight/..%2f..%2fsettings",
    "waikiki://open/beaconlight/meru/extra/segments",
])
def test_refuses_path_traversal(url):
    """A deep link must never be able to address a path we didn't construct."""
    assert deeplink.resolve(url) is None


def test_a_page_named_settings_is_a_page_not_the_settings_route():
    """`/wiki/settings` is a wiki page; `/settings` is the owner-only app route.

    A wiki is allowed to contain a page called "settings" -- resolving it must
    address the page view and drop any caller-supplied query.
    """
    got = deeplink.resolve("waikiki://open/beaconlight/settings?x=1")
    assert got == "/wiki/settings?wiki=beaconlight"
    assert "x=1" not in got


@pytest.mark.parametrize("url", [
    "waikiki://open/BEACONLIGHT/Meru",          # slugs are lowercase
    "waikiki://open/beacon light/meru",         # space
    "waikiki://open/beaconlight/me%2Fru",       # encoded slash
    "waikiki://open/-leading-dash/meru",
    "waikiki://open/beaconlight/meru#Bad Anchor",
    "waikiki://open/beaconlight/meru#../../x",
])
def test_refuses_slugs_and_anchors_that_are_not_slugs(url):
    assert deeplink.resolve(url) is None


def test_refuses_an_overlong_slug():
    assert deeplink.resolve(f"waikiki://open/{'a' * 200}/meru") is None


@pytest.mark.parametrize("url", [
    "waikiki://search",
    "waikiki://search?q=",
    "waikiki://search?wiki=beaconlight",        # no terms
    "waikiki://search?q=x&wiki=../../etc",
])
def test_refuses_bad_searches(url):
    assert deeplink.resolve(url) is None


def test_refuses_an_overlong_query():
    assert deeplink.resolve(f"waikiki://search?q={'x' * 300}") is None


def test_settings_is_not_reachable():
    """The whole point of the allow-list: owner-only surfaces stay unreachable."""
    for url in ("waikiki://settings", "waikiki://settings/updates/install",
                "waikiki://open/main/settings/updates/install",
                "waikiki://open/main/../settings"):
        got = deeplink.resolve(url)
        # Never the app's /settings tree. A page path (/wiki/settings) is fine.
        assert got is None or not got.startswith("/settings")


def test_no_resolution_ever_contains_a_host():
    """Paths are app-relative; the base is added by the caller, never by input."""
    for url in ("waikiki://open/beaconlight/meru", "waikiki://home",
                "waikiki://search?q=x"):
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
    reports the waikiki:// form instead.
    """
    from waikiki import mcp_server, store

    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    monkeypatch.setattr(mcp_server.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    store.create_page("Meru", "# Meru\n\nspirit")
    res = mcp_server.get_page("meru")
    assert res["link"] == "waikiki://open/main/meru"
    assert deeplink.resolve(res["link"]) == "/wiki/meru?wiki=main"


def test_page_view_offers_the_deep_link(wiki):
    """The Copy link button carries the scheme URL, not the http one."""
    from fastapi.testclient import TestClient

    from waikiki import store
    from waikiki.api import app

    store.create_page("Meru", "# Meru\n\nspirit")
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        body = c.get("/wiki/meru").text
    assert "waikiki://open/main/meru" in body and "wkCopyLink" in body
