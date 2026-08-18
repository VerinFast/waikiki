"""Doorman as host, and Doorman as the model behind the three AI surfaces.

Two halves of issue #63:

1. When Waikiki is displayed *inside* Doorman, "use Doorman when it is running"
   is not a question worth asking — Doorman is the host. The integration is
   forced on and the control is rendered locked, with the reason.
2. Generation, chat and images go to a Doorman agent when Doorman offers one,
   and to today's local path when it doesn't — silently, whether it says so with
   the documented quiet `{"available": false}` 200 or by 404ing the route
   because it predates it (0.19.9 in the field does).

Every Doorman response here is faked. Nothing in this file needs Doorman
installed, running, or reachable.
"""
import json

import anyio
import httpx
from fastapi.testclient import TestClient

from waikiki import ai, chat, clirun, doorman, imagegen, shellenv, store
from waikiki.api import app

HEALTH = {"ok": True, "version": "0.20.0",
          "capabilities": {"ask": True, "image": True, "tts": False}}


def _client():
    return TestClient(app, client=("127.0.0.1", 1))


def _doorman(monkeypatch, health=HEALTH, ask=None, image=None, calls=None):
    """Fake Doorman's GET probes. `ask`/`image` may be a dict, or an exception
    class to raise (a 404 from an older Doorman looks like a raised
    HTTPStatusError from `_get`)."""
    def fake_get(path, **kw):
        if calls is not None:
            calls.append(path)
        if path == "/api/health":
            if health is None:
                raise httpx.ConnectError("not running")
            return health
        if path == "/api/ask/status":
            if isinstance(ask, Exception):
                raise ask
            return ask if ask is not None else {"available": False}
        if path == "/api/image/status":
            if isinstance(image, Exception):
                raise image
            return image if image is not None else {"available": False}
        return {}

    monkeypatch.setattr(doorman, "_get", fake_get)


def _not_found() -> httpx.HTTPStatusError:
    """What `_get` raises against a Doorman that predates the endpoint."""
    request = httpx.Request("GET", "http://127.0.0.1:8900/api/ask/status")
    return httpx.HTTPStatusError("404", request=request,
                                 response=httpx.Response(404, request=request))


# --- fake HTTP for the POST paths -------------------------------------------

class _Resp:
    def __init__(self, status=200, headers=None, payload=None, lines=()):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self._lines = list(lines)
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def read(self):
        return self.content

    async def aread(self):
        return self.content

    def iter_lines(self):
        return iter(self._lines)

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _post_client(monkeypatch, resp, seen=None, is_async=False):
    """Point httpx at a canned response for POSTs, recording the bodies sent."""
    class C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, **k):
            if seen is not None:
                seen.append({"url": url, "body": json})
            return resp

        def post(self, url, json=None, **k):
            if seen is not None:
                seen.append({"url": url, "body": json})
            return resp

    monkeypatch.setattr(httpx, "AsyncClient" if is_async else "Client", C)


NDJSON = {"content-type": "application/x-ndjson"}


def _stream(*events):
    return _Resp(200, NDJSON, lines=[json.dumps(e) for e in events])


ANSWERED = (
    {"kind": "message", "role": "user", "text": "the question"},
    {"kind": "message", "role": "agent", "text": "Because of the reef."},
    {"kind": "status_update", "id": 1, "status": "read"},
    {"kind": "end"},
)


# =============================================================================
# Half 1 — embedded in Doorman
# =============================================================================

def test_a_doorman_iframe_is_embedded(wiki, monkeypatch):
    """Sec-Fetch-Dest says iframe and Doorman answers: it is the host."""
    _doorman(monkeypatch)
    assert doorman.embedded() is False
    doorman.note_request({"sec-fetch-dest": "iframe"})
    assert doorman.embedded() is True


def test_an_iframe_without_doorman_is_not_embedded(wiki, monkeypatch):
    """'Embedded' has to mean embedded *in Doorman*, not in any iframe — or any
    page on the internet could frame Waikiki and switch the integration on."""
    _doorman(monkeypatch, health=None)
    doorman.note_request({"sec-fetch-dest": "iframe"})
    assert doorman.embedded() is False
    assert doorman.enabled() is True          # unchanged: still the user's setting
    store.set_setting("doorman_enabled", "0")
    assert doorman.enabled() is False


def test_an_ordinary_request_is_not_embedded(wiki, monkeypatch):
    _doorman(monkeypatch)
    doorman.note_request({"sec-fetch-dest": "document"})
    assert doorman.embedded() is False


def test_a_framing_check_costs_nothing_when_nothing_frames_us(wiki, monkeypatch):
    """Standalone Waikiki must not probe Doorman on every request."""
    calls = []
    _doorman(monkeypatch, calls=calls)
    doorman.note_request({"user-agent": "x"})
    doorman.note_request({"sec-fetch-dest": "document"})
    assert calls == []


def test_embedded_overrides_a_user_who_declined(wiki, monkeypatch):
    """Declining is meaningful when Doorman merely runs; inside its window it is
    incoherent, so the integration is on."""
    _doorman(monkeypatch)
    store.set_setting("doorman_enabled", "0")
    assert doorman.enabled() is False
    doorman.note_request({"sec-fetch-dest": "iframe"})
    assert doorman.enabled() is True
    assert doorman.preference() is False      # their choice is remembered, not rewritten


def test_the_marker_a_future_doorman_might_send_is_honoured(wiki, monkeypatch):
    _doorman(monkeypatch)
    doorman.note_request({}, query="embed=doorman&wiki=main")
    assert doorman.embedded() is True


def test_settings_locks_the_control_when_embedded(wiki, monkeypatch):
    """Locked with a reason, not hidden: a control that vanishes is worse."""
    _doorman(monkeypatch)
    with _client() as c:
        body = c.get("/settings", headers={"sec-fetch-dest": "iframe"}).text
    assert "disabled" in body
    assert "Waikiki is running inside Doorman" in body
    assert "Use Doorman when it is running" in body, "hidden, not locked"


def test_settings_stays_a_free_choice_in_a_plain_window(wiki, monkeypatch):
    _doorman(monkeypatch)
    with _client() as c:
        body = c.get("/settings").text
    assert "Waikiki is running inside Doorman" not in body
    assert "Use Doorman when it is running" in body


def test_the_locked_setting_cannot_be_posted_off(wiki, monkeypatch):
    """The lock is enforced server-side, not only in the markup."""
    _doorman(monkeypatch)
    doorman.note_request({"sec-fetch-dest": "iframe"})
    with _client() as c:
        r = c.post("/settings/doorman", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert store.get_setting("doorman_enabled", "1") == "1"
    assert doorman.enabled() is True


# =============================================================================
# Half 2 — capability probing
# =============================================================================

def test_an_older_doorman_404s_and_that_is_not_an_error(wiki, monkeypatch):
    """0.19.9 in the field has none of these routes. A 404 is treated exactly
    like the documented quiet {'available': false} 200 — never version-sniffed."""
    _doorman(monkeypatch, ask=_not_found(), image=_not_found())
    assert doorman.can_ask() is False
    assert doorman.can_image() is False
    assert doorman.ask_backend() is None


def test_the_quiet_not_configured_200_is_a_no(wiki, monkeypatch):
    _doorman(monkeypatch, ask={"available": False, "reason": "no agent configured"},
             image={"available": False, "reason": "no image model"})
    assert doorman.can_ask() is False
    assert doorman.can_image() is False


def test_a_configured_doorman_is_a_yes_with_a_name(wiki, monkeypatch):
    _doorman(monkeypatch, ask={"available": True, "platform": "claude",
                               "agent": "Claude Code", "agent_id": "cc"})
    assert doorman.can_ask() is True
    assert doorman.ask_backend()["label"] == "Doorman · Claude Code"


def test_a_missing_capability_is_probed_once_not_per_page(wiki, monkeypatch):
    """Cached both ways, like the health probe: a missing capability costs one
    short-timeout request, not one per render."""
    calls = []
    _doorman(monkeypatch, ask=_not_found(), calls=calls)
    for _ in range(5):
        doorman.can_ask()
    assert calls.count("/api/ask/status") == 1


def test_healths_capability_hint_saves_the_probe(wiki, monkeypatch):
    """Doorman's health payload already answers 'could you at all?' for free."""
    calls = []
    _doorman(monkeypatch, health={"ok": True, "capabilities": {"ask": False}},
             calls=calls)
    assert doorman.can_ask() is False
    assert "/api/ask/status" not in calls


# =============================================================================
# Half 2 — chat
# =============================================================================

def _page(wiki_name="main"):
    store.create_page("Reef", "# Reef\nA reef is a ridge.")


def _no_clis(monkeypatch):
    """No claude/gemini/agy on this machine, so any answer came from Doorman."""
    monkeypatch.setattr(shellenv, "which", lambda name: None)


def test_chat_asks_a_doorman_agent_when_there_is_one(wiki, monkeypatch):
    _page()
    _no_clis(monkeypatch)
    _doorman(monkeypatch, ask={"available": True, "agent": "Claude Code"})
    seen = []
    _post_client(monkeypatch, _stream(*ANSWERED), seen)

    out = chat.answer("reef", "Why is it there?")
    assert out["ok"] is True
    assert out["answer"] == "Because of the reef."
    assert out["backend"] == "doorman"
    assert out["label"] == "Doorman · Claude Code"
    # wiki + page are what keep Doorman's audited conversations separate.
    assert seen[0]["url"].endswith("/api/ask")
    assert seen[0]["body"]["wiki"] == "main"
    assert seen[0]["body"]["page"] == "reef"


def test_chat_falls_back_to_the_cli_when_doorman_404s(wiki, monkeypatch):
    """An older Doorman must leave chat exactly as it is today."""
    _page()
    _doorman(monkeypatch, ask=_not_found())
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)
    ran = []

    def fake_run(label, argv, timeout):
        ran.append(label)

        class P:
            stdout = "the local CLI answered"
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    out = chat.answer("reef", "Why is it there?")
    assert ran == ["claude:chat"]
    assert out["answer"] == "the local CLI answered"
    assert out["backend"] == "claude"


def test_chat_falls_back_on_the_quiet_not_configured_200(wiki, monkeypatch):
    _page()
    _doorman(monkeypatch, ask={"available": False, "reason": "no agent configured"})
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)

    def fake_run(label, argv, timeout):
        class P:
            stdout = "the local CLI answered"
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    assert chat.answer("reef", "q?")["backend"] == "claude"


def test_chat_falls_back_when_the_ask_itself_declines(wiki, monkeypatch):
    """The status probe said yes 20 seconds ago; the POST says no. Still quiet."""
    _page()
    _doorman(monkeypatch, ask={"available": True, "agent": "Claude Code"})
    _post_client(monkeypatch, _Resp(200, {"content-type": "application/json"},
                                    payload={"available": False, "reason": "gone"}))
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)

    def fake_run(label, argv, timeout):
        class P:
            stdout = "the local CLI answered"
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    assert chat.answer("reef", "q?")["backend"] == "claude"


# =============================================================================
# Half 2 — generation
# =============================================================================

def _generate(prompt="write about reefs"):
    """Collect the generation stream. `anyio.run` like the rest of the suite —
    there is no async pytest plugin here."""
    async def go():
        return [e async for e in ai.stream_events(prompt, None, False, "reef")]

    return anyio.run(go)


def test_generation_streams_from_a_doorman_agent(wiki, monkeypatch):
    _doorman(monkeypatch, ask={"available": True, "agent": "Claude Code"})
    seen = []
    _post_client(monkeypatch, _stream(*ANSWERED), seen, is_async=True)

    events = _generate()
    assert events[0] == {"backend": "doorman", "label": "Doorman · Claude Code"}
    assert "".join(e.get("text", "") for e in events) == "Because of the reef."
    assert seen[0]["body"]["page"] == "reef" and seen[0]["body"]["wiki"] == "main"


def test_generation_falls_back_when_doorman_404s(wiki, monkeypatch):
    """Anthropic/Ollama exactly as today — and the editor is told which."""
    _doorman(monkeypatch, ask=_not_found())
    store.set_setting("gen_provider", "ollama")
    store.set_setting("gen_model_local", "phi3")

    async def fake_local(system, user_message):
        yield "local "
        yield "tokens"

    monkeypatch.setattr(ai, "_ollama_stream", fake_local)
    events = _generate()
    assert events[0] == {"backend": "ollama", "label": "Ollama · phi3"}
    assert "".join(e.get("text", "") for e in events) == "local tokens"


def test_generation_falls_back_on_the_quiet_200(wiki, monkeypatch):
    _doorman(monkeypatch, ask={"available": False, "reason": "no agent configured"})
    store.set_setting("gen_provider", "ollama")

    async def fake_local(system, user_message):
        yield "local"

    monkeypatch.setattr(ai, "_ollama_stream", fake_local)
    events = _generate()
    assert events[0]["backend"] == "ollama"


def test_generation_falls_back_when_the_ask_declines_mid_flight(wiki, monkeypatch):
    _doorman(monkeypatch, ask={"available": True, "agent": "Claude Code"})
    _post_client(monkeypatch, _Resp(200, {"content-type": "application/json"},
                                    payload={"available": False, "reason": "gone"}),
                 is_async=True)
    store.set_setting("gen_provider", "ollama")

    async def fake_local(system, user_message):
        yield "local"

    monkeypatch.setattr(ai, "_ollama_stream", fake_local)
    events = _generate()
    assert events[0]["backend"] == "ollama"
    assert "".join(e.get("text", "") for e in events) == "local"


# =============================================================================
# Half 2 — images
# =============================================================================

PNG = b"\x89PNG\r\n\x1a\nfake"


def _image_reply():
    import base64
    return _Resp(200, {"content-type": "application/json"},
                 payload={"available": True, "b64": base64.b64encode(PNG).decode(),
                          "mime": "image/png", "model": "gpt-image-1"})


def test_images_render_through_doorman_when_it_can(wiki, monkeypatch):
    _page()
    _no_clis(monkeypatch)                       # no agy CLI: Doorman or nothing
    _doorman(monkeypatch, ask={"available": False},
             image={"available": True, "model": "gpt-image-1"})
    seen = []
    _post_client(monkeypatch, _image_reply(), seen)

    out = imagegen.generate("reef", "a wave")
    assert out["ok"] is True
    assert out["label"] == "Doorman · gpt-image-1"
    assert out["markdown"].startswith("![a wave](/image/")
    assert seen[0]["url"].endswith("/api/image")
    # The file also lands in the article folder, which is what keeps a later
    # local render stylistically consistent with it.
    assert (imagegen.article_image_dir("main", "reef") / "a-wave-1.png").read_bytes() == PNG


def test_images_fall_back_to_the_cli_when_doorman_404s(wiki, monkeypatch):
    _page()
    _doorman(monkeypatch, ask=_not_found(), image=_not_found())
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)
    ran = []

    def fake_run(label, argv, timeout):
        ran.append(label)
        if label.endswith(":image"):
            (imagegen.article_image_dir("main", "reef") / "out-1.png").write_bytes(PNG)

        class P:
            stdout = "a prompt"
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    out = imagegen.generate("reef", "a wave", image_cli="agy")
    assert out["ok"] is True and "agy:image" in ran
    assert out["label"] == "agy CLI"


def test_images_fall_back_on_the_quiet_not_configured_200(wiki, monkeypatch):
    _page()
    _doorman(monkeypatch, ask={"available": False},
             image={"available": False, "reason": "no image model"})
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)

    def fake_run(label, argv, timeout):
        if label.endswith(":image"):
            (imagegen.article_image_dir("main", "reef") / "out-1.png").write_bytes(PNG)

        class P:
            stdout = "a prompt"
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    assert imagegen.generate("reef", "a wave", image_cli="agy")["label"] == "agy CLI"


def test_a_real_doorman_image_failure_is_shown_not_swallowed(wiki, monkeypatch):
    """Doorman distinguishes 'not configured' (quiet) from 'tried and failed'
    (502). The second is meant to be seen."""
    _page()
    _no_clis(monkeypatch)
    _doorman(monkeypatch, ask={"available": False},
             image={"available": True, "model": "gpt-image-1"})
    _post_client(monkeypatch, _Resp(502, {"content-type": "application/json"},
                                    payload={"error": "provider exploded"}))
    out = imagegen.generate("reef", "a wave")
    assert out["ok"] is False and "provider exploded" in out["error"]


# =============================================================================
# Standalone Waikiki is untouched
# =============================================================================

def test_no_doorman_means_todays_behaviour_everywhere(wiki, monkeypatch):
    """The common case: nothing probes past health, nothing changes."""
    calls = []
    _doorman(monkeypatch, health=None, calls=calls)
    assert doorman.can_ask() is False
    assert doorman.can_image() is False
    assert ai.backend() == ai.local_backend()
    assert calls == ["/api/health"]
