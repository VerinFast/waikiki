# Syncing with Kahala

Kahala is the multi-tenant, server-side half of the same wiki (good-place
`services/kahala`). Waikiki can **link** a local wiki to one there, **push** it
up, **pull** it back down, or **clone** a remote wiki into a new local one.

Both ends speak the shared, version-gated `wiki-interchange` format — Kahala
through `packages/wiki-interchange`, Waikiki through its
[vendored copy](vendoring.md) — so nothing here invents a protocol. This is only
the wire and the credential.

* `waikiki/kahala.py` — the link record and the transfers.
* `waikiki/kahalaauth.py` — OpenID Connect, authorization code + PKCE.
* `waikiki/secretstore.py` — the Keychain, and the refusal to use anything else.

## What travels

Content, and only content (CLAUDE.md rule 7): pages as canonical Y.Docs, the
hierarchy **by slug**, manual order and starred flags, custom elements,
templates with their metadata schemas, and one copy of each distinct image blob.

Never `tenant_id` or `wiki_id`. Kahala re-attaches those from the caller's own
server-resolved scope, and the format's `ServerFieldLeakError` guard refuses a
payload carrying them — so a local push cannot steer where it lands or escalate
into another tenant's wiki. Embeddings are never shipped either: they are
derived data, regenerated locally on import.

## Push and pull both merge. Neither deletes.

This is the single most important thing to understand about the feature, and the
UI says it next to the buttons rather than only here.

A snapshot round-trip merges by slug on both ends. A push adds and updates pages
on Kahala and removes nothing there; a pull does the same locally. So:

* a page you deleted locally is **still on Kahala** after a push, and
* the next pull **brings it back**.

Calling this "upload" or "sync" would set up exactly the surprise issue #58
warned about, so it isn't called that anywhere in the interface. Deleting a page
on both sides is a deliberate act on both sides.

Incremental sync — exchanging state vectors and shipping only the missing
updates — is a separate, later piece of work. Kahala already exposes the
whole-wiki changelog routes for it; Waikiki's vendored interchange copy does not
yet carry the `WikiStateVector` / `WikiChangelog` types they use, so re-syncing
the vendored library is a prerequisite. Clone-then-push works without it.

## Signing in

Kahala authenticates against Keycloak natively (good-place decision D5), so
Waikiki is an OIDC **public client**: `good-place-waikiki`, standard flow with
PKCE S256 and **no client secret**. A desktop binary cannot keep a secret —
anyone holding the `.app` holds it — so PKCE, not a secret, is what binds an
authorization code to the app that asked for it.

The redirect comes back to **`http://127.0.0.1:<port>/kahala/callback`**, a
route on Waikiki's own server. Two reasons:

* There is no second listener to start, no port race, and no window in which an
  unrelated socket is open.
* RFC 8252 §7.2 warns that any other app on the machine can claim a custom URL
  scheme and intercept the code. Waikiki's `waikiki://` scheme is an
  [allow-list of page destinations](deep-links.md) precisely because it is an
  unauthenticated external input; an auth callback has no business going through
  it, and an early draft of the Kahala-side chart registered a
  `goodplace-waikiki://` scheme that no build of this app has ever claimed.

The redirect URI always uses `127.0.0.1`, never `config.HOST`, which becomes
`0.0.0.0` when LAN sharing is on — not an address a browser can return to.

## Where the refresh token lives, and why not anywhere else

In the **macOS Keychain**, under service `Waikiki`, keyed by the realm's issuer.

The two obvious alternatives are both wrong:

* **The wiki's `settings` table.** The wiki file *is* the export — "Save wiki"
  copies that database and `store.export_wiki_bundle` reads from it — so a token
  there is handed to whoever you share a wiki with.
* **`app_config.json`.** It never leaves on its own, but it is plaintext in the
  data directory and lands in Time Machine and any folder backup.

The link record (address + remote wiki name) is not secret and lives in
`data/wikis.json`, in the registry entry for the wiki. That is still *outside*
the wiki file, so a shared wiki does not carry a pointer at your Kahala.

### What the Keychain actually buys

Stated plainly, because overclaiming here would be its own bug: it protects the
token **at rest and off the machine**. It is not in a file you can accidentally
export, mail, or commit, and a stolen copy of the data directory contains nothing
to sign in with.

It does **not** protect against code already running as you — a same-user
process can shell out to `security` exactly as we do. That is the same trust
boundary the rest of the app sits on (`auth.py` grants loopback callers owner
rights), so it is not a new exposure.

The secret never appears in `argv`: `security -i` takes its commands on stdin,
so the token is not visible in `ps` while it is written.

### No silent fallback

If there is no secure store — a future Linux or Windows build, or a Mac without
`security` — signing in is **refused and reported**, and the capabilities pane
says so. There is no file backend to fall back to, not even a hidden one:
writing a credential to a plain file "just for now" is the outcome this design
exists to prevent.

## Refusals

Every one of these is a normal state that reports itself and changes nothing
locally. They are deliberately *different messages*, because they need different
actions from the reader.

| What happened | What Waikiki says |
|---|---|
| Not linked / not signed in | Which of the two it is, before any request is made |
| **3xx redirect** | Reported, **never followed** — see below |
| 401 | The sign-in was not accepted, and may have expired |
| 403 | Signed in, but not an owner/admin of that wiki on Kahala |
| 404 | No such wiki *that this account can see* — Kahala answers identically for a wiki that doesn't exist and one in another tenant, so the message names both possibilities |
| 409 | Refused as incompatible rather than merged; the two ends are on different interchange versions |
| Unreachable host | Named, with "nothing was changed here" |

### Redirects are never followed

`httpx` follows redirects by default, and a followed redirect **re-sends the
`Authorization` header** to whatever host the response names. Since every
request here carries a bearer token, that is a credential handed to a third
party. Every client in `kahala.py` and `kahalaauth.py` sets
`follow_redirects=False` and treats a 3xx as a refusal naming the location.

`tests/test_kahala_sync.py` pins this by asserting the token never reaches the
redirect target — flip that argument to `True` and the test goes red.

## Who may do what

* **The person at the machine** may do everything, from the Kahala pane.
* **A LAN guest may do none of it.** `/kahala` is in `auth._GUEST_DENY_PREFIX`.
  A guest who could reach `/kahala/link` or `/kahala/clone` could aim this
  machine at a Kahala *they* name and push the owner's wiki to it — exfiltration,
  not misconfiguration.
* **An agent (MCP)** gets `kahala_status`, `kahala_push` and `kahala_pull`, and
  deliberately *not* linking, cloning or signing in. Those three establish a
  destination or a credential, which is the owner's call — and signing in needs
  their browser regardless. An agent may move content along a route the owner
  set up; it may not create one.

* **A web page you happen to be visiting** may not either. Waikiki has no CSRF
  tokens — loopback callers are owner, so any site can POST to `127.0.0.1` and be
  obeyed. Across the rest of the app the worst that does is local damage, which
  is a known accepted risk. Here it is not the same risk: a forged
  `/kahala/link` followed by a forged `/kahala/push` would send the whole wiki to
  a server the attacker names. So every mutating `/kahala` POST is checked for
  `Sec-Fetch-Site` (falling back to `Origin` vs `Host`) and refused when it did
  not come from Waikiki's own pages. The check lives in the ASGI middleware, not
  in each route, so a route added later cannot forget it — and
  `tests/test_kahala_sync.py` enumerates the routes off the app rather than by
  hand for the same reason.

  The general gap is worth closing on its own terms; this PR guards the surface
  that turns it from local damage into exfiltration, and does not silently
  change how every other route behaves.

Push and pull go through the same `kahala` module for both surfaces, per rule 5,
so a human and an agent get the same transfer, the same gates and the same words.

## Configuration

Both default to the good-place realm and are overridable in `app_config.json`
for another deployment:

| Key | Default |
|---|---|
| `kahala_issuer` | `https://app.kwirker.com/kc/realms/good-place` |
| `kahala_client_id` | `good-place-waikiki` |

The Kahala address and remote wiki name are per-wiki and set in the UI.

## The far end

Kahala's routes are in `services/kahala/kahala/routes/interchange.py`:

* `GET  /api/interchange/wikis/{wiki}/snapshot` — clone/pull (streamed)
* `POST /api/interchange/wikis/{wiki}/snapshot` — push (multipart, OWNER/ADMIN)

It enforces scope translation, the admin gate on bulk push, and the version gate
server-side, because a peer cannot enforce them. The desktop client it admits —
the public `good-place-waikiki` Keycloak client, whose audience mapper stamps
`good-place-kahala` so Kahala's existing bearer acceptor takes it — is
good-place/platform issue #63.
