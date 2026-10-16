# Waikiki became Jamgraf

*A standing decision, recorded 2026-10-14. If the code and this disagree, the code
is wrong.*

The product was renamed from **Waikiki** to **Jamgraf** in v1.1.0, all the way down:
the package, the bundle, the bundle id, the data folder, the Keychain service, the
link scheme, the cookies and the environment variables.

Doing that inside this repo is the easy part. The old names are also written into
places this repo cannot edit, and each one keeps saying the old name until somebody
changes it by hand:

- a Claude Desktop config,
- doorman's settings,
- a browser's cookie jar,
- a link pasted into somebody's notes,
- an MCP server that has been running since before the upgrade.

So a pure rename passes every test while abandoning every existing install. This
document is the list of what the rename had to keep working, and why none of it can
be removed casually.

## The rule

**Every old name is still READ. None is ever WRITTEN.** New installs never see the
old names. Old installs keep working, and they move to the new names whenever a value
is next written. An old name stays until whatever still says it has been changed. That
is usually a file on somebody else's machine, so removing one is its own decision,
never a tidy-up.

## What is still read under the old name

| Old name | Where it is still said | Read by |
|---|---|---|
| `WAIKIKI_*` env vars | Claude Desktop configs (`WAIKIKI_MCP`, `WAIKIKI_DATA`, `WAIKIKI_WEB_URL`), doorman (`WAIKIKI_HEADLESS`) | `config.env()`. The new name wins, and an empty new value is *not* overruled by an old one |
| `~/Library/Application Support/Waikiki` | `WAIKIKI_DATA` in every Claude Desktop config | A symlink to `…/Jamgraf`, left by the first-launch move |
| Keychain service `Waikiki` | the Kahala sign-in of every pre-rename install | `secretstore.get_secret`, which moves it on first read |
| `Waikiki.app` | `/Applications`, the Dock, Claude Desktop configs, doorman | A symlink to `Jamgraf.app`, left by `updater.finish_bundle_rename` |
| `Contents/MacOS/Waikiki` | the `command` of every pre-rename MCP config | A symlink to `Jamgraf` inside the bundle, made by `build_macos.sh` |
| `waikiki://` links | every "Copy link" and MCP `get_page` before v1.1.0 | `deeplink.ACCEPTED_SCHEMES`. Both schemes are registered in the spec and go through the same allow-list |
| `X-Waikiki-Wiki` header | an MCP server started before the upgrade | `api._resolve_wiki` |
| `waikiki_*` cookies, `waikiki_rail_w` | a browser on the LAN or on `127.0.0.1` | `config.cookie()`, and `base.html` for the client-side ones |
| `waikiki_share` cookie | a LAN guest's sign-in | `auth.LEGACY_COOKIE`. It is accepted, **and deleted at logout** |
| `waikiki_version` key | an agent or script reading the MCP `version` tool | `mcp_server.version`, which returns both keys |
| Bonjour name `Waikiki` | a crashed advertiser left behind by a pre-rename run | `bonjour.start`, which reaps both names |
| `WAIKIKI_UPDATE_PUBKEY` | anyone's shell | `release.sh` **refuses** it, exactly like the new name |

Two of these are more than courtesy:

- **The data-folder symlink is what stops a quiet disaster.** `config.py` creates its
  data folder at import. Without the symlink, a stale `WAIKIKI_DATA` would be handed a
  fresh, *empty* install where the wikis used to be, and nothing would say where they
  went.
- **The `WAIKIKI_UPDATE_PUBKEY` refusal is security.** The app reads the old spelling as
  a fallback. Refusing only the new spelling would reopen the trapdoor that #103 closed:
  a release that verifies against whatever key the environment names.

## What deliberately kept its old value

None of these are names; they are identifiers. Changing them buys nothing, or breaks
something:

- **`PUBLIC_KEYS_HEX`.** Every installed copy refuses an update that isn't signed by a
  key it already trusts.
- **The `waikikey` secret** in Google Secret Manager. Renaming a secret means creating a
  new one and moving the key into it.
- **The `good-place-waikiki` Keycloak client**, and good-place's `kahala.waikiki.*`
  values key. The client is registered server-side. Renaming the key would silently
  drop every deploy override.
- **The `.wiki` manifest tag `waikiki-zip-1`.** It is a file-format tag that imports
  never check.
- **`waikiki.db`**, the legacy single-database file. A pre-multi-wiki install left it
  under that name, long before the rename.
- **The vendored `wiki_interchange`.** It is a pinned upstream copy (rule 7).
- **`docs/rfc/0001-multi-tenant-waikiki.md`**, a historical document.

## The first launch after upgrading

**The data folder** (`config.migrate_legacy_data_dir`), before anything else touches it:

| What's on disk | What happens |
|---|---|
| Old folder only | Renamed to `Jamgraf`, and the old path is left as a symlink. If the symlink can't be made, the rename is undone: all or nothing |
| Old folder, plus an *empty* new one | The empty one is removed with `rmdir`, which refuses anything non-empty, then the move goes ahead as above |
| Both hold wikis | **Neither is touched.** The new one is used, and Manage wikis says where the old one is |
| Old path already a symlink | Nothing to do |

The move is serialised with `flock`, because the desktop app and an MCP server can start
in the same instant. Open files survive it, since a rename moves the inode, not the
file.

**The Kahala sign-in** moves lazily, the first time it is read. It is written under
`Jamgraf`, *read back*, and only then deleted from `Waikiki`. A failed move costs a
retry, never the sign-in. Sign-out deletes under both, or the old item would come back
on the next read.

**The bundle.** The old updater swaps a new bundle into the path it is *running* from. So
the release that carries the rename lands as Jamgraf's code in a folder still called
`Waikiki.app`. The first desktop launch renames the folder and leaves `Waikiki.app` as a
symlink. This never happens in MCP mode, it is never fatal, and a half-done rename is
undone.

**The Help wiki's prose.** A page still exactly as the old version seeded it gets the
new text. A page somebody edited only has the product name, the repo and the link scheme
replaced. Both cases are versioned, and the system prompts are never touched.

## Shipping it

The order matters, because installed copies find updates at `VerinFast/waikiki`:

1. Merge this, then rename the GitHub repo to `VerinFast/jamgraf`. Every installed copy
   follows GitHub's redirect, because both of the updater's HTTP clients use
   `follow_redirects=True`. That is verified back to v0.14.0, the first release that can
   update itself.
2. **Never create a new repo called `VerinFast/waikiki`.** That kills the redirect, and
   every pre-rename install stops finding updates for good.
3. Release v1.1.0, signed with the same key.

Every installed copy accepts it: the updater takes the first `*.zip` and the first
`*.app`, and nothing checks the bundle id.

`tests/test_rename_compat.py` and `tests/test_rename_migration.py` hold all of the above.
Every guard in them was checked by removing it.
