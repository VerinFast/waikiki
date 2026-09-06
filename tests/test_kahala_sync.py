"""Kahala sync (issue #58): the credential, the wire, and the refusals.

This is an **auth + outbound network** path, so most of what is worth testing
here is what it refuses to do. Two properties are load-bearing enough that the
tests are written to fail if someone "simplifies" them away:

* a bearer token is never handed to a host we did not mean to talk to, which in
  practice means a 3xx is *reported*, never followed (``httpx`` re-sends the
  ``Authorization`` header across a redirect);
* the refresh token never lands in a wiki file or in ``app_config.json``, only
  in the Keychain -- because the wiki file *is* what "Save wiki" exports.

The HTTP fake replaces only the **transport**, never ``kahala._client`` itself.
That is deliberate: patching the client would also replace the arguments the
client is constructed with, and ``follow_redirects=False`` is one of those
arguments. Faking a layer lower means the redirect test exercises the real
setting -- flip it to True in ``kahala._client`` and this file goes red.
"""
from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from waikiki import (config, db, kahala, kahalaauth, secretstore, store,
                     wikis)
from waikiki.vendor import wiki_interchange as wi


# --- fakes -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Never touch the developer's actual Keychain, in either direction."""
    vault: dict[str, str] = {}
    monkeypatch.setattr(secretstore, "available", lambda: True)
    monkeypatch.setattr(secretstore, "set_secret",
                        lambda a, s: vault.__setitem__(a, s) or True)
    monkeypatch.setattr(secretstore, "get_secret", lambda a: vault.get(a))
    monkeypatch.setattr(secretstore, "delete_secret",
                        lambda a: vault.pop(a, None) is not None)
    kahalaauth.forget()
    yield vault
    kahalaauth.forget()


@pytest.fixture(autouse=True)
def _no_network_by_default(monkeypatch):
    """Structural, like conftest's update-check guard: no accidental egress.

    A test that wants HTTP installs a handler with ``http``. Anything else that
    reaches the network fails loudly instead of quietly hitting a real host.
    """
    real = httpx.Client

    def refuse(*a, **kw):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(httpx, "Client", refuse)
    return real


@pytest.fixture
def http(monkeypatch, _no_network_by_default):
    """Install a request handler, keeping the real client's own arguments."""
    real = _no_network_by_default
    seen: list[httpx.Request] = []

    def install(handler):
        def make(*a, **kw):
            def wrapped(request):
                seen.append(request)
                return handler(request)
            kw["transport"] = httpx.MockTransport(wrapped)
            return real(*a, **kw)
        monkeypatch.setattr(httpx, "Client", make)
        return seen

    install.seen = seen
    return install


def _signed_in(monkeypatch):
    monkeypatch.setattr(kahalaauth, "access_token", lambda: "test-access-token")


def _bundle_of(slug: str) -> bytes:
    """A real interchange bundle produced from a real local wiki."""
    token = db.current_wiki.set(slug)
    try:
        return store.export_wiki_bundle()
    finally:
        db.current_wiki.reset(token)


# --- the credential ----------------------------------------------------------


def test_the_refresh_token_never_lands_in_a_wiki_or_in_app_config(
        wiki, http, monkeypatch, tmp_path):
    """The wiki file IS the export, so a token in it is a token you hand out."""
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(200, json={"access_token": "at",
                                         "refresh_token": "SECRET-REFRESH",
                                         "expires_in": 300})
    http(handler)

    url, state = kahalaauth.begin()
    assert "code_challenge_method=S256" in url
    assert kahalaauth.complete("the-code", state)["ok"]

    # Check the settings table through SQL, not by reading the .db file: in WAL
    # mode a just-written row still lives in the -wal sidecar, so a raw byte
    # scan of the .db passes while the token is very much in the wiki. (That is
    # not hypothetical -- the first version of this test did exactly that and
    # stayed green with the token stored via `store.set_setting`.)
    assert not any("SECRET-REFRESH" in str(v)
                   for v in store.all_settings().values()), \
        "the refresh token is in the wiki's settings table"

    # Then the claim that actually matters: it is not in what "Save wiki" hands
    # over. This is the real export path, WAL checkpointed and all.
    saved = tmp_path / "shared.wiki"
    wikis.export_to("main", str(saved))
    assert b"SECRET-REFRESH" not in saved.read_bytes(), \
        "the refresh token travels inside an exported wiki, which is exactly " \
        "what gets handed to whoever you share it with"

    cfg = config.DATA_DIR / "app_config.json"
    if cfg.exists():
        assert "SECRET-REFRESH" not in cfg.read_text()
    assert "SECRET-REFRESH" not in (config.DATA_DIR / "wikis.json").read_text()


def test_no_secure_store_means_no_sign_in_rather_than_a_file(monkeypatch):
    monkeypatch.setattr(secretstore, "available", lambda: False)
    ok, why = kahalaauth.can_sign_in()
    assert not ok and "Keychain" in why
    with pytest.raises(ValueError):
        kahalaauth.begin()


def test_a_sign_in_state_is_single_use(wiki, http, monkeypatch):
    """A replayed callback must not complete a second sign-in."""
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    http(lambda r: httpx.Response(200, json={
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token",
        "access_token": "at", "refresh_token": "rt", "expires_in": 300}))
    _url, state = kahalaauth.begin()
    assert kahalaauth.complete("code", state)["ok"]
    again = kahalaauth.complete("code", state)
    assert not again["ok"] and "already used" in again["error"]


def test_an_unknown_state_is_refused(wiki):
    out = kahalaauth.complete("code", "never-minted-this")
    assert not out["ok"]


# --- the token is never handed to another host -------------------------------


def test_a_redirect_is_reported_not_followed(wiki, http, monkeypatch):
    """The whole point: httpx re-sends Authorization across a redirect.

    Constructing the client with ``follow_redirects=True`` makes this pass a
    302 through to ``evil.example`` with the bearer token attached, so this test
    is what pins that argument in ``kahala._client``.
    """
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kahala.example":
            return httpx.Response(302, headers={
                "location": "https://evil.example/collect"})
        return httpx.Response(200, content=b"should never get here")

    seen = http(handler)
    out = kahala.pull("main")

    assert not out["ok"]
    assert "redirect" in out["error"].lower()
    hosts = {r.url.host for r in seen}
    assert "evil.example" not in hosts, \
        "the redirect was followed, so the bearer token was sent to evil.example"


def test_an_http_address_is_refused_before_any_request(wiki):
    out = kahala.link("main", "http://kahala.example", "remote-wiki")
    assert not out["ok"] and "https" in out["error"]
    assert wikis.get_link("main") is None


def test_loopback_http_is_allowed_for_local_development():
    assert kahalaauth.secure_url("http://127.0.0.1:8787")
    assert kahalaauth.secure_url("https://kahala.example")
    assert not kahalaauth.secure_url("http://kahala.example")
    assert not kahalaauth.secure_url("ftp://kahala.example")


def test_every_request_carries_the_bearer_token(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    seen = http(lambda r: httpx.Response(500, json={"detail": "nope"}))
    kahala.push("main")
    assert seen and all(
        r.headers.get("authorization") == "Bearer test-access-token"
        for r in seen)


# --- the link record ---------------------------------------------------------


def test_the_link_lives_in_the_registry_not_in_the_wiki(wiki):
    """Otherwise it travels to whoever you share the wiki with."""
    assert kahala.link("main", "https://kahala.example", "remote-wiki")["ok"]

    reg = json.loads((config.DATA_DIR / "wikis.json").read_text())
    entry = [w for w in reg["wikis"] if w["slug"] == "main"][0]
    assert entry["kahala"]["remote"] == "remote-wiki"

    assert b"kahala.example" not in wikis.db_path("main").read_bytes(), \
        "the Kahala address was written into the wiki file, so it would " \
        "travel with an exported wiki and point someone else's copy at it"


def test_unlink_forgets_only_the_link(wiki):
    kahala.link("main", "https://kahala.example", "remote-wiki")
    store.create_page("Kept", "still here")
    kahala.unlink("main")
    assert wikis.get_link("main") is None
    assert store.get_page("kept") is not None


def test_status_reports_without_touching_the_network(wiki, monkeypatch):
    monkeypatch.setattr(kahalaauth, "signed_in", lambda: False)
    out = kahala.status("main")
    assert out["linked"] is False and out["signed_in"] is False
    kahala.link("main", "https://kahala.example", "remote-wiki")
    assert kahala.status("main")["remote"] == "remote-wiki"


# --- the transfers -----------------------------------------------------------


def test_push_sends_a_real_bundle_to_the_snapshot_route(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    store.create_page("Travelling", "content that should be in the bundle")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body["url"] = str(request.url)
        body["raw"] = request.read()
        return httpx.Response(200, json={"pages": 1, "images": 0})

    http(handler)
    out = kahala.push("main")

    assert out["ok"], out.get("error")
    assert body["url"] == \
        "https://kahala.example/api/interchange/wikis/remote-wiki/snapshot"
    # It is a real interchange bundle, not an empty multipart shell.
    start = body["raw"].index(b"PK\x03\x04")
    names = zipfile.ZipFile(
        io.BytesIO(body["raw"][start:body["raw"].rindex(b"PK\x05\x06") + 22])
    ).namelist()
    assert "manifest.json" in names
    assert "pages/travelling.snapshot" in names, \
        f"the page never made it into the bundle: {names}"
    assert "deleted" not in out["note"].lower() or "Nothing" in out["note"]


def test_pull_merges_and_deletes_nothing_local(wiki, http, monkeypatch):
    """A pull must never remove a local page the remote doesn't have."""
    _signed_in(monkeypatch)
    other = wikis.create_wiki("Remote Source")
    token = db.current_wiki.set(other)
    try:
        db.init_db()
        store.create_page("From Kahala", "arrived from the server")
    finally:
        db.current_wiki.reset(token)
    payload = _bundle_of(other)

    store.create_page("Only Local", "must survive the pull")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(200, content=payload))

    out = kahala.pull("main")
    assert out["ok"], out.get("error")
    assert store.get_page("only-local") is not None, \
        "a local page the remote never had was removed by a pull"
    assert store.get_page("from-kahala") is not None


def test_clone_creates_a_new_wiki_and_links_it(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    other = wikis.create_wiki("Remote Source")
    token = db.current_wiki.set(other)
    try:
        db.init_db()
        store.create_page("Cloned Page", "hello")
    finally:
        db.current_wiki.reset(token)
    payload = _bundle_of(other)

    before = {w["slug"] for w in wikis.list_wikis()}
    http(lambda r: httpx.Response(200, content=payload))
    out = kahala.clone("https://kahala.example", "remote-wiki", "My Clone")

    assert out["ok"], out.get("error")
    fresh = out["wiki"]
    assert fresh not in before
    assert wikis.get_link(fresh)["remote"] == "remote-wiki"


def test_a_failed_clone_leaves_no_empty_wiki_behind(wiki, http, monkeypatch):
    """An empty wiki reads as "it worked and there was nothing there"."""
    _signed_in(monkeypatch)
    before = {w["slug"] for w in wikis.list_wikis()}
    http(lambda r: httpx.Response(404, json={"detail": "No such wiki"}))

    out = kahala.clone("https://kahala.example", "missing", "Doomed")
    assert not out["ok"]
    assert {w["slug"] for w in wikis.list_wikis()} == before, \
        "a failed clone left a wiki behind, which reads as an empty success"


def test_an_unreachable_kahala_changes_nothing_here(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    store.create_page("Local", "untouched")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    def boom(request):
        raise httpx.ConnectError("no route to host")

    http(boom)
    out = kahala.pull("main")
    assert not out["ok"] and "Couldn't reach" in out["error"]
    assert store.get_page("local") is not None


# --- the refusals say which one it was ---------------------------------------


@pytest.mark.parametrize("code,expect", [
    (401, "expired"),
    (403, "owner or admin"),
    (404, "another tenant"),
    (409, "incompatible"),
])
def test_each_refusal_says_what_to_do_about_it(wiki, http, monkeypatch,
                                               code, expect):
    """Four different problems must not collapse into one message."""
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(code, json={"detail": "refused"}))

    out = kahala.push("main")
    assert not out["ok"]
    assert expect in out["error"], f"{code} said: {out['error']}"


def test_a_version_mismatch_is_refused_whole(wiki, http, monkeypatch):
    """A bundle we cannot read must not half-apply."""
    _signed_in(monkeypatch)
    store.create_page("Local", "untouched")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(200, content=b"not a bundle at all"))

    out = kahala.pull("main")
    assert not out["ok"] and "refused" in out["error"]
    assert store.get_page("local") is not None


def test_push_and_pull_refuse_before_the_network_when_not_linked(wiki):
    assert "isn't linked" in kahala.push("main")["error"]
    assert "isn't linked" in kahala.pull("main")["error"]


def test_they_refuse_when_signed_out(wiki, monkeypatch):
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    monkeypatch.setattr(kahalaauth, "access_token", lambda: None)
    assert "signed in" in kahala.push("main")["error"]


# --- a LAN guest may not use any of this -------------------------------------


@pytest.mark.parametrize("path", [
    "/kahala", "/kahala/signin", "/kahala/callback", "/kahala/push",
    "/kahala/pull", "/kahala/clone", "/kahala/link", "/kahala/signout",
])
def test_a_guest_cannot_reach_the_kahala_routes(path):
    """Owner-only, and not merely for tidiness.

    A guest who could reach ``/kahala/clone`` or ``/kahala/link`` could point
    this machine at a Kahala *they* name and push the owner's wiki to it. That
    is exfiltration, not a misconfiguration, so it sits with the other
    owner-only paths in ``auth``.
    """
    from waikiki import auth
    assert not auth.guest_may(path), \
        f"a LAN guest can reach {path}, which is enough to send a wiki off-box"


# --- the pane renders --------------------------------------------------------


def test_the_kahala_pane_renders_in_every_state(wiki, monkeypatch):
    """A template error only shows at render time, so render it.

    Both states matter: signed-out (the sign-in button and no link form) and
    linked (the push/pull buttons plus the merge warning), because the second
    branch is the one nothing else in this file touches.
    """
    from fastapi.testclient import TestClient
    from waikiki.api import app

    monkeypatch.setattr(kahalaauth, "signed_in", lambda: True)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.get("/kahala")
        assert out.status_code == 200
        assert "Sign out" in out.text

        wikis.set_link("main", "https://kahala.example", "remote-wiki")
        out = client.get("/kahala?wiki=main")
        assert out.status_code == 200
        assert "Push to Kahala" in out.text
        # The one sentence that keeps a merge from being a surprise.
        assert "neither deletes" in out.text.lower()


def test_the_pane_says_why_when_there_is_no_secure_store(wiki, monkeypatch):
    from fastapi.testclient import TestClient
    from waikiki.api import app

    monkeypatch.setattr(secretstore, "available", lambda: False)
    monkeypatch.setattr(secretstore, "unavailable_reason",
                        lambda: "No secure place to keep a sign-in token.")
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.get("/kahala")
        assert out.status_code == 200
        assert "No secure place" in out.text
        assert "Sign in to Kahala" not in out.text, \
            "a sign-in button is offered that cannot possibly work"


# --- cross-site forgery ------------------------------------------------------


def _kahala_post_routes() -> list[str]:
    """Every mutating Kahala route, read off the app rather than hand-listed.

    Hand-listing is how a route added next year quietly ships unguarded.
    """
    from waikiki.api import app
    return sorted({r.path for r in app.routes
                   if getattr(r, "path", "").startswith("/kahala")
                   and "POST" in getattr(r, "methods", set())})


def test_there_are_kahala_post_routes_to_check():
    """Guards the guard: an empty list would make the next test vacuous."""
    assert len(_kahala_post_routes()) >= 5


@pytest.mark.parametrize("path", _kahala_post_routes())
def test_a_cross_site_post_is_refused(wiki, path):
    """Loopback is owner, so any web page can POST here and be obeyed.

    For the rest of the app that risks local damage. Here a forged link+push
    would send the whole wiki to a host the attacker chose, so it is refused.
    """
    from fastapi.testclient import TestClient
    from waikiki.api import app

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.post(path, data={"wiki": "main", "base_url":
                                      "https://evil.example", "remote": "x"},
                          headers={"sec-fetch-site": "cross-site"})
        assert out.status_code == 403, \
            f"a cross-site POST to {path} was accepted"

        # And the same request from Waikiki's own page is not blocked by this.
        ours = client.post(path, data={"wiki": "main", "base_url":
                                       "https://kahala.example", "remote": "x"},
                           headers={"sec-fetch-site": "same-origin"},
                           follow_redirects=False)
        assert ours.status_code != 403, \
            f"{path} refuses Waikiki's own form, so the feature is unusable"


# --- incremental sync ---------------------------------------------------------
#
# The bundle ships every page in full; a re-sync of a real wiki that differs by
# a paragraph pushes ~57MB. These pin the cheap path AND the two ways it is
# allowed to give up: a peer too old to carry definitions, and images that did
# not survive the trip. Both must fall back rather than leave the wiki subtly
# wrong, and both must SAY they fell back.


def _remote_wiki_with(pages=(("Alpha", "first body"),), element=True):
    """A second local wiki standing in for Kahala's copy."""
    from waikiki import elements

    other = wikis.create_wiki("Remote Source")
    token = db.current_wiki.set(other)
    try:
        db.init_db()
        if element:
            elements.save_element("card", "Card", [{"name": "t"}],
                                  "<b></b>", ".card{}", "")
            store.template_save("Report", "# {{title}}")
        for title, body in pages:
            store.create_page(title, body)
    finally:
        db.current_wiki.reset(token)
    return other


def _as(slug, fn):
    token = db.current_wiki.set(slug)
    try:
        return fn()
    finally:
        db.current_wiki.reset(token)


def test_the_state_vector_and_changelog_move_definitions_not_just_pages(wiki):
    """The whole point of spec v3, exercised through the repository."""
    from waikiki import elements

    remote = _remote_wiki_with()
    peer_sv = _as("main", store.wiki_state_vector)
    log = _as(remote, lambda: store.wiki_changelog_for(peer_sv))

    assert [e.slug for e in log.elements] == ["card"]
    assert "Report" in [t.name for t in log.templates]
    assert log.carries_definitions

    summary = _as("main", lambda: store.apply_wiki_changelog(log))
    assert summary["elements"] == 1
    assert _as("main", lambda: elements.get_element("card")) is not None, \
        "the page arrived but the element it renders with did not"
    assert _as("main", lambda: store.get_page("alpha")) is not None


def test_a_second_pass_resends_nothing(wiki):
    """If digests didn't work, every sync would ship every definition forever."""
    remote = _remote_wiki_with()
    first = _as(remote, lambda: store.wiki_changelog_for(
        _as("main", store.wiki_state_vector)))
    _as("main", lambda: store.apply_wiki_changelog(first))

    second = _as(remote, lambda: store.wiki_changelog_for(
        _as("main", store.wiki_state_vector)))
    assert second.elements == [] and second.templates == []
    assert not second.carries_definitions, \
        "an envelope with nothing new stamped the v3 floor, so an older peer " \
        "would reject it for no reason"


def test_an_incremental_pull_asks_for_a_changelog_and_applies_it(wiki, http,
                                                                 monkeypatch):
    _signed_in(monkeypatch)
    remote = _remote_wiki_with()
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/wiki-changelog")
        peer = wi.WikiStateVector.deserialize(request.read())
        log = _as(remote, lambda: store.wiki_changelog_for(peer))
        return httpx.Response(200, content=log.serialize())

    http(handler)
    out = kahala.pull("main")
    assert out["ok"], out.get("error")
    assert out["mode"] == "incremental"
    assert store.get_page("alpha") is not None


def test_an_incremental_push_sends_only_what_the_peer_lacks(wiki, http,
                                                            monkeypatch):
    _signed_in(monkeypatch)
    store.create_page("Local Only", "body")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wiki-state-vector"):
            # A peer that holds nothing, but does speak v3.
            return httpx.Response(200, content=wi.WikiStateVector().serialize())
        sent["log"] = wi.WikiChangelog.deserialize(request.read())
        return httpx.Response(200, json={"created": ["local-only"]})

    http(handler)
    out = kahala.push("main")
    assert out["ok"], out.get("error")
    assert out["mode"] == "incremental"
    assert "local-only" in [p.slug for p in sent["log"].pages]


def test_an_older_kahala_falls_back_to_the_whole_wiki_and_says_so(wiki, http,
                                                                  monkeypatch):
    """A v2 peer's envelope simply omits the sections — it never announces itself.

    Detecting that by the *absence of the key* rather than a version number is
    the load-bearing bit: a v3 peer with nothing new to send produces identical
    content, and only one of the two is safe to sync incrementally.
    """
    _signed_in(monkeypatch)
    remote = _remote_wiki_with()
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    payload = _bundle_of(remote)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wiki-changelog"):
            # Exactly what a pre-v3 Kahala answers: no definition sections.
            return httpx.Response(200, json={
                "format": "good-place.wiki-interchange/wiki-changelog",
                "spec_version": 1, "yjs_protocol": 1,
                "pages": [], "missing_from_server": []})
        return httpx.Response(200, content=payload)

    http(handler)
    out = kahala.pull("main")
    assert out["ok"], out.get("error")
    assert out["mode"] == "full"
    assert "older interchange" in out["note"], \
        "it silently used the slow path; a fallback nobody can see is one " \
        "nobody can question"


def test_a_real_error_does_not_quietly_retry_the_expensive_way(wiki, http,
                                                              monkeypatch):
    """Falling back on a genuine failure just fails again, slower."""
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(403, json={"detail": "nope"})

    http(handler)
    out = kahala.pull("main")
    assert not out["ok"] and "owner or admin" in out["error"]
    assert not any("snapshot" in c for c in calls), \
        "a 403 sent us round again for the whole wiki instead of reporting it"


def test_the_full_flag_skips_the_incremental_path_entirely(wiki, http,
                                                           monkeypatch):
    _signed_in(monkeypatch)
    remote = _remote_wiki_with()
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    payload = _bundle_of(remote)
    seen = http(lambda r: httpx.Response(200, content=payload))

    out = kahala.pull("main", full=True)
    assert out["ok"] and out["mode"] == "full"
    assert all("changelog" not in str(r.url) for r in seen)


def test_an_image_blob_that_lies_about_its_hash_is_refused(wiki):
    """Bytes and digest arrive together from a peer, so the digest proves nothing
    unless it is checked. Unverified, a payload lands under a trusted hash."""
    log = wi.WikiChangelog(images=[
        wi.BundleImage(sha256="ab" * 32, media_type="image/png", data=b"not that")])
    with pytest.raises(wi.MalformedEnvelopeError):
        store.apply_wiki_changelog(log)


def test_an_update_for_a_page_we_do_not_have_is_skipped_not_invented(wiki):
    """An incremental update cannot be applied to nothing."""
    log = wi.WikiChangelog(pages=[wi.WikiChangelogPage(
        slug="never-seen", changelog=wi.Changelog(ydoc_update=b"").serialize())])
    summary = store.apply_wiki_changelog(log)
    assert summary["skipped"] == ["never-seen"]
    assert store.get_page("never-seen") is None
