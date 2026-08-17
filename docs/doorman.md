# Doorman (optional)

[Doorman](https://github.com/VerinFast/doorman) is a sibling local app — same
pywebview + FastAPI + apsw shape — that bridges a human, an agent and the
machine. When it happens to be running, Waikiki can use it for things it does
less well. Implementation: [`waikiki/doorman.py`](../waikiki/doorman.py).

## The rule

**Optional in every direction.** Doorman's own `CLAUDE.md` states that a feature
must never require an out-of-app action, with integrations with the user's
separate apps as the explicit exception provided they stay clearly optional.
Waikiki holds itself to the same line:

- Detect it if it is there; **never** require it.
- **Never** start, install or prompt to install it. It is the user's app.
- Every feature works identically without it — Doorman is an upgrade, not a
  dependency.
- The user can decline the integration even while Doorman is running
  (**Settings → Doorman**), because running it and wanting Waikiki to reach into
  it are different things.
- Say nothing when it is absent. A machine without Doorman is the common case,
  not a misconfiguration.

`tests/test_doorman.py` enforces the parts that can be enforced, including a test
that `doorman.py` contains no process-spawning at all.

## Detection

`GET /api/health` on Doorman's local port (8900 by default, `DOORMAN_PORT` there,
`WAIKIKI_DOORMAN_URL` here). Cached for 30s **in both directions** — a negative
result is cached too, or a machine without Doorman would make a doomed request on
every page render.

## What is used today

**Speech.** `POST /api/tts` returns WAV, which sounds considerably better than the
browser's `speechSynthesis`. Used by **Listen**, the per-section speakers and
*"Say this word"*. On any hiccup the page silently falls back to the built-in
voice — a reader should never see an error for what is only a nicer option.

Note this needs no changes on Doorman's side: its TTS is already an HTTP
endpoint.

## What is not wired yet

**Agents.** Doorman has remote and local agents behind a tool bridge, and its MCP
server exposes `list_agents` / `send_to_agent`. Chat with a page could use those
instead of only a local `claude`/`gemini` CLI. That is a larger piece of work and
is not built.

If it is, wiki isolation still applies: a Doorman agent reading or writing a wiki
goes through the same per-session `switch_wiki` discipline as any other agent
(rule 4), with no shared or inherited active wiki.

## Direction of travel

Doorman already integrates the *other* way: `doorman/waikiki.py` connects to
Waikiki's MCP server so an agent there can drive the wiki, configured by its
`waikiki_mcp` setting. That is Doorman calling Waikiki. This document is the
reverse — Waikiki calling Doorman — and the two are independent.
