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
- Say *something* when it answers. Optional does not mean invisible: swapping
  the user's configured Anthropic model for a Doorman agent without saying so
  would be its own bug, so every surface that can be answered by Doorman carries
  a label naming what answered.

**One exception, and only one:** Waikiki displayed *inside* Doorman's own window.
See [Embedded in Doorman](#embedded-in-doorman) below.

`tests/test_doorman.py` enforces the parts that can be enforced, including a test
that `doorman.py` contains no process-spawning at all.

## Detection

`GET /api/health` on Doorman's local port (8900 by default, `DOORMAN_PORT` there,
`WAIKIKI_DOORMAN_URL` here). Cached for 30s **in both directions** — a negative
result is cached too, or a machine without Doorman would make a doomed request on
every page render.

### Capability probing, never version sniffing

Doorman gained `/api/ask` and `/api/image` in 0.20.0. The Doorman in the field
does not have them and **404s** those routes. Waikiki never looks at a version
number to decide: it asks `/api/ask/status` and `/api/image/status`, and treats a
404 exactly like the documented quiet `{"available": false, "reason": ...}` 200 —
"can't, no harm done, use your own path". Those probes are cached for 30s both
ways too, so a missing capability costs one short-timeout request rather than one
per page render. `/api/health` also carries free `capabilities: {ask, image, tts}`
hints, and a `false` there saves even that request.

## Embedded in Doorman

Doorman embeds Waikiki in a plain iframe pointed at its `waikiki_url` setting.
Nothing in that request says "embedded", so Settings used to offer *"Use Doorman
when it is running"* as a checkbox while Waikiki was running **inside** Doorman —
incoherent, since Doorman is unmistakably present and is the host.

The decision is needed server-side (`doorman.enabled()` gates the integration and
Settings renders server-side), so a client-side `window.self !== window.top`
check cannot be the authority. Waikiki uses two signals together:

1. **`Sec-Fetch-Dest: iframe`** on the document request — the browser says it,
   needs no change in Doorman, and can't be spoofed by the framing page itself.
   A `?embed=doorman` marker is honoured too, for a future Doorman that sends one.
2. **The health probe** — because (1) only says *an* iframe. Requiring Doorman to
   actually answer is what makes it mean *Doorman's* iframe, so a random page
   framing Waikiki can't switch the integration on.

Only the framed *document* request carries that header; the fetch() calls the
framed page then makes do not. So the judgement is remembered in `doorman.py`
(refreshed by every in-frame navigation, expiring 15 minutes after the last one)
rather than re-derived per request. It is a property of the window Waikiki is
displayed in — unlike the active wiki (CLAUDE.md rule 4), there is nothing here
that could leak between callers.

When embedded, `enabled()` returns True regardless of the stored preference, and
Settings renders the checkbox **locked, with the reason** rather than hiding it —
a control that silently disappears is more confusing than one that explains
itself. `POST /settings/doorman` refuses the change too, so the lock isn't only
in the markup. The user's own preference is remembered untouched, and applies
again the moment Waikiki is opened on its own.

## What is used today

**Speech.** `POST /api/tts` returns WAV, which sounds considerably better than the
browser's `speechSynthesis`. Used by **Listen**, the per-section speakers and
*"Say this word"*. On any hiccup the page silently falls back to the built-in
voice — a reader should never see an error for what is only a nicer option.

Note this needs no changes on Doorman's side: its TTS is already an HTTP
endpoint.

**Generation and chat.** `POST /api/ask` — a one-shot question answered by a
Doorman agent, streamed back as NDJSON `bridge.drive` events (it is not a plain
string; Waikiki reads the `message`/`role: agent` events and ignores the rest of
Doorman's bookkeeping). Used by the editor's **Generate** button in place of
Anthropic/Ollama, and by **chat with a page** in place of the `claude`/`gemini`
CLI.

Waikiki sends `wiki` and `page` with every ask. Doorman backs the facade with a
real, audited conversation keyed per `(wiki, page, agent)`, so those are what keep
its conversations separate and legible. That path is the same governed one as its
own `/send`: `local:` directives still execute, under per-agent grants and
confirm/go. Waikiki does not try to route around that, and shouldn't.

Wiki isolation still applies: a Doorman agent reading or writing a wiki goes
through the same per-session `switch_wiki` discipline as any other agent
(rule 4), with no shared or inherited active wiki.

**Images.** `POST /api/image` renders one image with the model configured in
Doorman's BYOM settings; the returned bytes are saved into the article's own
image folder and ingested exactly like a CLI-generated one. The prompt that goes
to it is written by a Doorman agent too, so this path needs neither the `agy` CLI
nor a local `claude`.

One thing the Doorman path cannot carry: the wiki's **style reference images**.
`/api/image` takes a prompt, a model and a size, so only the style *prompt*
applies. The `--add-dir` reference trick remains a local-CLI feature.

Doorman distinguishes *not configured* (quiet 200 → Waikiki falls back to its own
CLI, silently) from *tried and failed* (400/502 → shown to the user). Waikiki
honours that distinction rather than flattening both into a silent fallback.

## Which backend answered

Routing lives in `doorman.py`, below the routes, so the HTML views, the REST API
and the MCP tools agree on it (rule 5) — `imagegen.generate` is the same function
for the editor's image panel and for the `generate_image` MCP tool.

Every surface reports what answered:

| surface | how it says so |
|---|---|
| Generation | the SSE stream's first frame carries `{backend, label}`; the editor shows it in the status line |
| Chat | the reply dict carries `backend` + `label`; the panel prints it under the answer |
| Images | the result dict carries `label`; the image panel says "Added by …" |
| Settings | **Settings → Doorman** names the agent and image model that would answer |

## Direction of travel

Doorman already integrates the *other* way: `doorman/waikiki.py` connects to
Waikiki's MCP server so an agent there can drive the wiki, configured by its
`waikiki_mcp` setting. That is Doorman calling Waikiki. This document is the
reverse — Waikiki calling Doorman — and the two are independent.
