# Updates

How the packaged `Waikiki.app` updates itself, and the trust model that makes
doing so safe. Implementation: [`waikiki/updater.py`](../waikiki/updater.py);
release tooling: [`scripts/release.sh`](../scripts/release.sh).

## Why it isn't just a download link

A running `.app` cannot overwrite its own bundle. So every update is:

```
check → download → verify → back up → stage → hand off → quit → swap → relaunch
```

The swap runs in a **detached helper script** (`start_new_session=True`) that
waits for the app's PID to disappear, moves the old bundle aside, `ditto`s the
new one into place, and relaunches. It has to outlive the process that spawned
it, which is why it's a script on disk and not a thread.

The app quits by sending itself `SIGTERM`, so FastAPI's lifespan shutdown runs on
the way out — that's what flushes `collab.py`'s pending CRDT snapshots. A hard
`os._exit` here would lose the tail of any live editing session.

That flush is explicit: the shutdown calls `collab.flush_all()` **before**
cancelling the flusher task. Cancelling alone is not enough and used to lose
data — `collab.flusher` only persists text that has been idle for
`_FLUSH_IDLE`, so anything typed in the last couple of seconds had never been
written, and cancelling simply stopped the loop rather than draining it. See
issue #19 and `tests/test_collab_shutdown.py`, which fails if the final flush is
removed.

Rollback is built into the helper: if `ditto` fails, it restores the bundle it
moved aside and relaunches that instead. The old bundle is only deleted after the
new one is in place.

## User data is never touched

Wikis live in `~/Library/Application Support/Waikiki` (`config.DATA_DIR`),
outside the bundle. An update replaces code only.

A backup still runs first, via `backups.run_backup()`. Schema migrations are
forward-only: a newer version will migrate a wiki on open, but an older binary
can't read it back. `page_ydoc` in 0.13.0 is exactly that kind of change. So the
backup is insurance against rolling back, not against the swap itself.

## Trust model — read this before changing anything here

The bundle carries no Apple identity. `waikiki.spec` sets
`codesign_identity=None`, and `build_macos.sh` applies an **ad-hoc** signature
(`codesign --sign -`), which proves nothing about origin — it's a signature with
no signer. macOS therefore gives us **no authenticity guarantee** about a
downloaded zip.

This code path downloads a blob and then executes it as the user. That makes it
the highest-privilege path in the app, and HTTPS is not sufficient: TLS protects
the transport, not the artifact. A compromised release, a hijacked redirect, or a
misconfigured mirror all produce a perfectly valid TLS download.

So Waikiki carries its own trust root:

- Every release zip is signed with an **Ed25519** key, over the zip's streamed
  **SHA-256 digest** rather than the archive bytes — so a ~100 MB download is
  verified in constant memory. `updater.file_digest` and `scripts/release.sh`
  must stay in lockstep; signing a hash is the standard construction, and
  substituting content would require a SHA-256 collision.
- The **public** half is pinned in `updater.PUBLIC_KEYS_HEX`, compiled into the
  app. It is a *set*: a release has to satisfy one of them.
- The **private** half never enters this repo. `release.sh` reads it from
  `WAIKIKI_UPDATE_KEY` (default `~/.waikiki/update-key.pem`) and refuses to use
  a key stored inside the working tree, where it could be committed by accident.
- Verification happens **before** the archive is expanded. An unverified payload
  never gets to write files.

It **fails closed**, in every direction:

| Situation | Result |
|---|---|
| No public key pinned in the build | Updating disabled entirely |
| Malformed pinned key (bad hex, wrong length) | Updating disabled |
| Signature missing, malformed, or truncated | Refused |
| Signature valid but from a different key | Refused |
| Payload modified after signing | Refused |
| Archive has no `.app`, or no `Contents/MacOS` | Refused |
| Bundle version disagrees with the release tag | Refused |
| Bundle not writable by this user | Refused, with the reason shown |
| Not running from a packaged `.app` | Refused |

Never populate `PUBLIC_KEYS_HEX` from the network, from `app_config.json`, or from
anything else a user or attacker can rewrite. A pinned key that can be replaced
is not a trust root. `WAIKIKI_UPDATE_PUBKEY` exists as an override for tests and
private builds — it is deliberately an environment variable, not a setting.

## Cutting a release

One-time, to create the signing key:

```bash
./scripts/release.sh --genkey
```

That prints the public half. Paste it into `updater.PUBLIC_KEYS_HEX`, commit, and
**back up the private key** — losing it means you cannot ship an update that
existing installs will accept. There is no recovery path short of users
reinstalling by hand.

Then, per release:

```bash
./scripts/release.sh v0.14.0
```

Which builds via `build_macos.sh`, signs the zip, and uploads both the zip and
its `.sig`. Add `--dry-run` to do everything except the upload.

Two guards worth knowing about, because they fail the release rather than ship a
broken one:

- The tag must match `waikiki.__version__`. A mismatch would make the updater's
  own staging check reject the build.
- The signature is verified **against the key pinned in the build being
  shipped**, not just against the private key that signed it. That catches a
  stale or empty `PUBLIC_KEYS_HEX` — the case that would otherwise publish a
  release every client refuses.

A release with no signed zip asset is a release nobody can install. The Settings
page says so explicitly rather than offering a button that fails at
verification.

## Behaviour in the app

Checks are **check-only**. The hourly maintenance loop in `api.py` (alongside the
trash sweep and backups) asks GitHub for the latest release at most once per
`update_interval_hours` and records an `UPGRADE available` line in the access
log. It never installs unattended: swapping the bundle restarts the app, and
doing that to someone mid-edit is not a decision to make on their behalf.

Installing is always an explicit click in **Settings → Updates**.

App-global settings live in `app_config.json` via `appconfig`, not per-wiki:

| Key | Default | Meaning |
|---|---|---|
| `update_auto_check` | `true` | Check automatically |
| `update_interval_hours` | `24` | Minimum hours between checks |
| `update_last_check` | — | Unix time of the last check |
| `update_last_seen` | — | Tag seen by that check, installable or not |
| `update_last_available` | — | Tag of the last **newer and signed** release |

`update_last_available` is the only one Settings may gate the install offer on.
Deriving it from `last_seen != current` would offer a **downgrade** when a local
build is newer than the latest release, and would offer an install for a release
with no `.sig` — one that must fail at verification.

## Known limits

- **First install is unchanged.** An unsigned app still needs right-click →
  **Open** the first time. Updates after that launch cleanly, because we download
  the zip ourselves and never set `com.apple.quarantine` (and strip it
  defensively while staging). Sparkle plus a Developer ID is the fix, when there
  is one.
- **Full-bundle only.** Every update ships the whole ~260 MB app. Deltas via a
  git-tracked app-code channel are the planned follow-up; see
  [issue #8](https://github.com/VerinFast/waikiki/issues/8) for the dependency
  trimming that shrinks the baseline either way.
- **No privileged install.** If the bundle isn't writable by the current user
  (root-owned in `/Applications`), the updater refuses and says why instead of
  reaching for an admin prompt or a privileged helper.
- **The swap itself is not unit-tested.** It happens in a detached helper that
  outlives the test process, so exercising it under pytest would mean swapping
  the app out from under the test runner. Everything that *decides* whether to
  swap is tested in `tests/test_updater.py`; the helper is covered by the
  `swap.log` it writes into `DATA_DIR/updates/`.


## Rotating the signing key

A build trusts the keys compiled into it for as long as it is installed. That
cuts both ways: lose the private half and those copies can never update again,
and a stolen key cannot be revoked — they will accept whatever it signs until
each is reinstalled by hand. Neither is recoverable after the fact, which is why
`PUBLIC_KEYS_HEX` is a set rather than one key, from the first public release.

Rotation is therefore an *overlap*, not a switch:

1. Append the new public key to `PUBLIC_KEYS_HEX`. Keep signing with the first
   one. Release. Now shipped copies trust both.
2. Wait for that build to spread. Anyone still on an older build trusts only the
   old key, and step 3 will strand them until they update.
3. Move the new key to index 0 and sign with it. `scripts/release.sh` prints
   which pinned key signed, and warns when it is not the first — that line going
   from `#0` to `#1` is the rotation happening.
4. Once the overlap build is everywhere, drop the retired key.

Step 1 is the one with a deadline: it only helps copies released *after* it, so
the set has to be in place before the installs you care about exist.
