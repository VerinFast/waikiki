import time

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from waikiki import appconfig, auth
from waikiki.api import ShareAuthMiddleware


def test_password_hash_and_verify(wiki):
    auth.set_password("hunter2")
    assert auth.has_password()
    assert auth.verify_password("hunter2")
    assert not auth.verify_password("hunter3")
    assert not auth.verify_password("")
    # the plaintext is never stored
    assert "hunter2" not in str(appconfig.get("share_hash"))


def test_clearing_password_disables_sharing(wiki):
    auth.set_password("x")
    appconfig.set("share_enabled", True)
    auth.set_password("")
    assert not auth.has_password() and not auth.enabled()


def test_token_roundtrip_expiry_and_tamper(wiki):
    auth.set_password("pw")
    tok = auth.make_token()
    assert auth.check_token(tok)
    assert not auth.check_token(tok + "x")          # tampered signature
    assert not auth.check_token("garbage")
    expired = f"{int(time.time()) - 10}.deadbeef"
    assert not auth.check_token(expired)


def test_password_change_invalidates_sessions(wiki):
    auth.set_password("first")
    tok = auth.make_token()
    assert auth.check_token(tok)
    auth.set_password("second")                      # rotates the signing key
    assert not auth.check_token(tok)


@pytest.mark.parametrize("path", [
    "/settings", "/settings/sharing", "/settings/models/add",
    "/elements", "/elements/foo/edit", "/wikis", "/wikis/main/delete",
    "/logs", "/debug", "/connect",
    "/wiki/x/chat", "/wiki/x/generate-image", "/wiki/x/purge",
])
def test_guest_denied_dangerous_paths(path):
    assert not auth.guest_may(path)


@pytest.mark.parametrize("path", [
    "/", "/wiki/reef", "/wiki/reef/edit", "/wiki/save", "/search",
    "/index", "/templates", "/tags", "/changes", "/trash", "/help",
])
def test_guest_allowed_content_paths(path):
    assert auth.guest_may(path)


def _app():
    app = FastAPI()

    @app.get("/wiki/reef")
    def page():
        return PlainTextResponse("page ok")

    @app.get("/settings")
    def settings():
        return PlainTextResponse("settings ok")

    app.add_middleware(ShareAuthMiddleware)
    return app


def test_loopback_is_owner_even_when_sharing_on(wiki):
    auth.set_password("pw")
    appconfig.set("share_enabled", True)
    c = TestClient(_app(), client=("127.0.0.1", 5555))
    assert c.get("/wiki/reef").text == "page ok"
    assert c.get("/settings").text == "settings ok"      # owner keeps full access


def test_network_caller_needs_password_then_is_limited(wiki):
    auth.set_password("pw")
    appconfig.set("share_enabled", True)
    c = TestClient(_app(), client=("192.168.1.50", 5555))

    r = c.get("/wiki/reef", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    c.cookies.set(auth.COOKIE, auth.make_token())
    assert c.get("/wiki/reef").text == "page ok"          # guest can read/edit pages
    r = c.get("/settings", follow_redirects=False)
    assert r.status_code == 403                            # but not run local commands


def test_network_blocked_entirely_when_sharing_off(wiki):
    auth.set_password("pw")
    appconfig.set("share_enabled", False)
    c = TestClient(_app(), client=("192.168.1.50", 5555))
    c.cookies.set(auth.COOKIE, auth.make_token())
    assert c.get("/wiki/reef", follow_redirects=False).status_code == 403


# --- Tunnel traffic must never be mistaken for the owner ----------------------

def test_proxy_headers_defeat_loopback_owner_check():
    """cloudflared connects over loopback; without this, internet traffic would
    arrive as 127.0.0.1 and be handed full owner access."""
    assert auth.is_local("127.0.0.1", {}) is True
    for hdr in ("x-forwarded-for", "cf-connecting-ip", "cf-ray", "x-real-ip",
                "forwarded", "cf-ipcountry"):
        assert auth.is_local("127.0.0.1", {hdr: "1.2.3.4"}) is False, hdr


def test_tunnel_visitor_is_a_guest_not_the_owner(wiki):
    auth.set_password("pw")
    appconfig.set("share_enabled", True)
    # Simulates cloudflared: loopback client, but carrying forwarding headers.
    c = TestClient(_app(), client=("127.0.0.1", 5555),
                   headers={"CF-Connecting-IP": "203.0.113.9"})
    r = c.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    c.cookies.set(auth.COOKIE, auth.make_token())
    assert c.get("/wiki/reef").text == "page ok"          # signed in: can read
    assert c.get("/settings", follow_redirects=False).status_code == 403  # not owner


def test_tunnel_alone_enables_remote_access(wiki, monkeypatch):
    """With LAN sharing off but a tunnel up, remote callers are still gated."""
    from waikiki import tunnel
    auth.set_password("pw")
    appconfig.set("share_enabled", False)
    assert auth.enabled() is False
    monkeypatch.setattr(tunnel, "is_running", lambda: True)
    assert auth.enabled() is True
    auth.set_password("")                                  # no password, no access
    assert auth.enabled() is False
