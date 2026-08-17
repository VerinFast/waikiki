"""The Doorman integration must be optional in every direction.

Doorman is the user's separate app. Waikiki may use it when it happens to be
running, must never require it, must never start or install it, and must keep
working identically when it is absent.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from waikiki import doorman, store
from waikiki.api import app


@pytest.fixture(autouse=True)
def _fresh_cache():
    doorman._cache.update(at=0.0, info=None)
    yield
    doorman._cache.update(at=0.0, info=None)


def _client():
    return TestClient(app, client=("127.0.0.1", 1))


def test_absent_doorman_is_normal_not_an_error(wiki, monkeypatch):
    """A machine without Doorman is the common case and must be silent."""
    def refuse(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(doorman, "_get", refuse)
    assert doorman.available() is False
    assert doorman.speak("hello") is None
    assert doorman.voices() == {}
    assert doorman.status()["running"] is False


def test_a_missing_doorman_is_probed_once_not_per_page(wiki, monkeypatch):
    """A negative result is cached too, or every render pays for a doomed request."""
    calls = []

    def refuse(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(doorman, "_get", refuse)
    for _ in range(5):
        doorman.available()
    assert len(calls) == 1


def test_detection_uses_doormans_health_endpoint(wiki, monkeypatch):
    monkeypatch.setattr(doorman, "_get",
                        lambda path, **k: {"ok": True, "version": "1.2.3"}
                        if path == "/api/health" else {})
    assert doorman.available() is True
    assert doorman.status()["version"] == "1.2.3"


def test_the_user_can_decline_even_when_doorman_runs(wiki, monkeypatch):
    """Running it and wanting Waikiki to reach into it are different things."""
    monkeypatch.setattr(doorman, "_get", lambda path, **k: {"ok": True})
    assert doorman.available() is True
    store.set_setting("doorman_enabled", "0")
    doorman._cache.update(at=0.0, info=None)
    assert doorman.available() is False
    assert doorman.speak("hello") is None


def test_speech_falls_back_rather_than_failing(wiki, monkeypatch):
    """A bad response means "use the built-in voice", never an error a reader sees."""
    monkeypatch.setattr(doorman, "_get", lambda path, **k: {"ok": True})

    class R:
        status_code = 500
        content = b""

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return R()

    monkeypatch.setattr(httpx, "Client", C)
    assert doorman.speak("hello") is None


def test_the_voice_route_404s_when_unavailable(wiki, monkeypatch):
    """404 is 'no better voice', which the page treats as fall back."""
    monkeypatch.setattr(doorman, "speak", lambda *a, **k: None)
    with _client() as c:
        r = c.post("/api/voice/speak", json={"text": "hello"})
    assert r.status_code == 404


def test_the_voice_route_returns_audio_when_available(wiki, monkeypatch):
    monkeypatch.setattr(doorman, "speak", lambda *a, **k: b"RIFFfake")
    with _client() as c:
        r = c.post("/api/voice/speak", json={"text": "hello"})
    assert r.status_code == 200 and r.content == b"RIFFfake"
    assert r.headers["content-type"] == "audio/wav"


def test_settings_shows_it_as_optional(wiki, monkeypatch):
    monkeypatch.setattr(doorman, "_get",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no")))
    with _client() as c:
        body = c.get("/settings").text
    assert "Doorman" in body and "Everything works without it" in body


def test_waikiki_never_launches_doorman(wiki):
    """It is the user's app to start. Nothing here may spawn or install it."""
    import inspect

    src = inspect.getsource(doorman)
    for forbidden in ("subprocess", "Popen", "os.system", "osascript", "open -a"):
        assert forbidden not in src, f"doorman.py must not {forbidden}"
