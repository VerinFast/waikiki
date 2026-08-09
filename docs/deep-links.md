# Deep links (`waikiki://`)

A stable link that opens the desktop app at a specific place. Implementation:
[`waikiki/deeplink.py`](../waikiki/deeplink.py) (parsing + allow-list) and
`_install_deeplink_handler` in [`waikiki_app.py`](../waikiki_app.py) (the macOS
event handler).

## Forms

| Link | Opens |
|---|---|
| `waikiki://open/<wiki>/<page>` | that page |
| `waikiki://open/<wiki>/<page>#<section>` | that page, scrolled to a heading |
| `waikiki://open/<wiki>` | the wiki's front page |
| `waikiki://search?q=<terms>` | search results |
| `waikiki://search?q=<terms>&wiki=<wiki>` | search, scoped to one wiki |
| `waikiki://home` | the front page |

`<section>` is a heading's anchor slug — the same value `render.py` puts on
headings, and what MCP `get_page` returns in `outline`.

## Why not just an http:// URL

`waikiki_app._pick_port` scans for a free port at startup, so an install serving
`127.0.0.1:8787` today can be on `8788` tomorrow. An http link embeds that port
and goes stale; `waikiki://` doesn't.

This is the reason the feature exists: an agent holding a page wants to hand a
human something openable. MCP `get_page` therefore returns a `link` field with
the scheme URL, and **Page options → Copy link** copies the same thing.

## Security — read before adding a verb

A registered URL scheme is an **unauthenticated external input**. Any web page,
mail message, or other app can fire `waikiki://…` and macOS will deliver it to
us. There is no origin, no user gesture we can verify, and no authentication.

That matters more here than in most apps, because the desktop window loads over
loopback and [`auth.py`](../waikiki/auth.py) grants loopback callers **owner**
rights. A design that forwarded the incoming URL's path to `load_url` would let
any website drive owner-level routes inside someone's wiki.

So `deeplink.resolve()`:

- accepts exactly three verbs (`open`, `search`, `home`) and refuses everything else
- **constructs** the resulting path itself; it never echoes a caller-supplied path
- validates every slug against `^[a-z0-9][a-z0-9-]{0,127}$` — no dots, slashes, or
  percent escapes, so nothing can traverse a path or smuggle a second component
- validates section anchors the same way, and refuses the whole link on a bad one
- returns an **app-relative** path only. The loopback base is prepended by the
  caller, never taken from input, so a link can't redirect the window off-host
- caps query length and refuses an empty search

Adding a verb widens what the entire internet can ask this app to do. The
refusal cases in [`tests/test_deeplink.py`](../tests/test_deeplink.py) are the
load-bearing half of that file.

Note that `waikiki://open/<wiki>/settings` is *allowed* — it resolves to
`/wiki/settings`, a wiki page that happens to be named "settings". That is not
the app's owner-only `/settings` route, and the tests pin the distinction.

## How the URL reaches the app

macOS delivers a scheme open as the `GURL` Apple Event, so:

1. `waikiki.spec` registers `CFBundleURLSchemes: ["waikiki"]` in the bundle's
   `Info.plist`. **Without this macOS never routes the URL to us** — and it only
   takes effect in a packaged build, not a source run.
2. `_install_deeplink_handler` registers a handler with `NSAppleEventManager`
   for that event, keeping a module-level reference (the manager holds only a
   weak one to the target).
3. The handler resolves the URL through `deeplink.resolve` and, only on success,
   calls `load_url(base + path)`. A refused link is logged to stderr.

Every failure in that path is swallowed: deep links are a convenience, and a
missing pyobjc symbol must never stop the app from launching.

## Limits

- **Packaged app only.** A `python waikiki_app.py` source run has no `Info.plist`,
  so macOS has no scheme registration to route. The parsing layer is fully
  testable without a build; the delivery layer isn't.
- **One registration wins.** If several Waikiki builds exist on the machine,
  LaunchServices picks one — usually the most recently registered. Testing a dev
  build while a released app is installed can open the wrong one.
- **No cross-machine meaning.** A link names a wiki and a page, not a host. Sent
  to someone else it opens *their* wiki of that name, or fails. These are local
  links, not shareable URLs — that's a job for the LAN/tunnel sharing in
  Settings.
