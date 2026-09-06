# Vendored packages

Waikiki ships as a standalone desktop app, so a dependency that is **not
published to PyPI** is vendored into the source tree rather than declared in
`requirements.txt`. Each vendored tree is a verbatim copy of an upstream package
at a recorded revision, with the pinned version recorded in three places that
must stay in lockstep:

- `waikiki/vendor/__init__.py` — `WIKI_INTERCHANGE_VERSION` /
  `WIKI_INTERCHANGE_REVISION` / `_SOURCE`
- `requirements.txt` — the "VENDORED, not on PyPI" note
- this document

## `wiki_interchange`

| | |
|---|---|
| **Path** | `waikiki/vendor/wiki_interchange/` |
| **Pinned version** | `0.2.0` (spec `SPEC_VERSION = 3`, floor `1`, Yjs sync protocol `1`) |
| **Upstream** | `gitlab.kwirker.com/good-place/platform`, `packages/wiki-interchange/wiki_interchange/` (**private repo** — first-party, same owner; the vendored copy here is the public one, under this project's Elastic License 2.0). GitLab is canonical; the GitHub mirror carries the same code but issues and MRs live on GitLab. |
| **Upstream branch** | `claude/wiki-changelog-carries-elements` (MR good-place/platform!142, targeting `dev`) — vendored ahead of merge, as the spec-v2 sync was |
| **Upstream revision** | `42c1fa22` — the vendored tree is byte-identical to `packages/wiki-interchange/wiki_interchange/` at this commit |
| **Runtime dep** | `pycrdt>=0.10,<0.15` (satisfied by Waikiki's own pin) |

### What it is

`wiki_interchange` is the **shared, content-only Y.Doc interchange format** for
the Kahala ⟷ Waikiki round-trip (RFC [atd-v3-kahala], decision D3: the format is
a small library both sides vendor + version-pin, not a service and not on PyPI).
Waikiki consumes it through `waikiki/ydoc.py`:

- `build_page_doc` / the root-type constants — the canonical page Y.Doc layout
  (`content` Text, `tree`/`comments`/`tags`/`elements` Arrays, `meta` Map).
- `encode_snapshot` / `decode_snapshot` — full Y.Doc + content-addressed
  `ImageRef` sidecar (**produce**/**consume**), for one page.
- `pack_bundle_into` / `BundleReader` — the **whole-wiki bundle** (spec v2): every
  page's snapshot, the hierarchy by slug, order, starred, custom elements,
  templates with their metadata schemas, and one copy of each distinct image
  blob. Both ends stream, one page at a time.
- `state_vector` / `produce_changelog` / `apply_changelog` — per-page
  incremental sync.
- `WikiStateVector` / `WikiChangelog` / `WikiChangelogPage` — the same handshake
  at **whole-wiki** granularity: a peer publishes what it holds for every page,
  and gets back per-page updates for pages it has plus full snapshots for pages
  it lacks. What these envelopes **do not carry** is load-bearing — see below.
- `check_compatible` / `negotiate` — the spec + Yjs-protocol version gate that
  **rejects** an incompatible envelope rather than merging bad bytes.

The format carries **content only** — no `tenant_id` / `wiki_id` / permissions
(server-owned, re-attached by Kahala) and no RAG chunks/embeddings (derived,
regenerated locally on import). The vendored guards enforce this on both encode
and decode.

### What each envelope stamps (spec v2)

`SPEC_VERSION` is the highest spec this build understands; what a payload
*carries* is the oldest spec that can read **its kind** — `PAGE_ENVELOPE_SPEC`
(1: a page snapshot/changelog is unchanged since v1) or `WIKI_ENVELOPE_SPEC` (2).
That split is why bumping the spec here does not break the per-page round-trip
against a **deployed** Kahala: its image is pinned by content hash, so a spec-2
Waikiki routinely meets a spec-1 peer, and a page snapshot stamped with the
producer's build number would be rejected by a peer that understands it perfectly.
`MIN_COMPATIBLE_SPEC_VERSION` stays `1` — v2 is additive, so this build still
reads v1 payloads.

### What the wiki changelog carries (spec v3)

Until v3, `WikiChangelog` was `pages` + `missing_from_server` and a per-page
`Changelog` was a bare `ydoc_update` — so **images, custom elements and
templates did not travel incrementally at all**. Pages a peer had never seen
went as full `Snapshot`s, which do carry an image sidecar, so a *first* transfer
looked complete and the *later* incremental ones quietly drifted: pages current,
definitions stale, nothing reporting a problem.

v3 gives the changelog those sections, and gives the state vector digests of
what the sender already holds so only the difference travels. One thing it still
cannot fix by itself: a per-page changelog is a Yjs update that names the
*sender's* image ids, so `store.apply_wiki_changelog` remaps them from the
envelope's `image_ids` after the merge, and `kahala.py` falls back to a full
bundle if any reference still fails to resolve. See `docs/kahala-sync.md`.

### Version and revision are not the same pin

Upstream shipped the whole-wiki changelog — a new module and five new exports —
**without moving `__version__` off `0.2.0`**. So the version string cannot tell
you the vendored copy is stale, which is precisely how it went stale here. When
checking, diff the tree or compare `WIKI_INTERCHANGE_REVISION` against
upstream's latest commit for that path; move the revision on every re-sync even
when the version has not changed.

### Import atomicity (known limit)

`store.import_wiki_bundle` decodes the **whole** payload before its first write
(`store._read_bundle`), so a malformed, version-incompatible or tampered bundle —
the realistic failure mode, since it arrives from a peer — leaves the wiki
untouched. A failure of the local *writes* (full disk, lock) can still leave a
partial import; making that atomic too means staging into a scratch database and
swapping the file, which is deliberately out of scope for issue #57.

The 1.0 audit measured what that partial state actually is and **decided against
staging-and-swap** for 1.0: the import merges by slug and never deletes, so a
failed run leaves an *incomplete but not destructive* wiki, every page that
landed is a normal versioned page, and re-running the same bundle finishes it
exactly. See `docs/data-safety.md` question 4 for the numbers and the trade-off;
`tests/test_data_safety.py` pins the property.

### Why vendored (not a dependency)

The desktop app bundles its own Python; there is no package index at install
time, and the library must stay byte-for-byte in lockstep with the in-monorepo
Kahala service through the version gate. Vendoring makes the pin explicit and the
build self-contained.

### Re-sync steps

When W1 (or a later spec bump) changes upstream:

1. Pull the package tree from `main` (GitLab is canonical):

   ```sh
   GITLAB_HOST=gitlab.kwirker.com glab api \
     "projects/good-place%2Fplatform/repository/files/\
   packages%2Fwiki-interchange%2Fwiki_interchange%2F<file>/raw?ref=main"
   # fetch each file's raw content into waikiki/vendor/wiki_interchange/
   ```

   Then `diff` every file against the vendored copy — the tree must be
   byte-identical, and the diff is also how you find out *what* moved.

2. Copy the `wiki_interchange/` package over `waikiki/vendor/wiki_interchange/`
   (keep only the package — not its `pyproject.toml`/`tests/`, which stay
   upstream).
3. Read the new `__version__` from upstream's `wiki_interchange/__init__.py`
   **and** the commit you synced from, and update the pin in **all three**
   places listed at the top of this file. The revision moves every time; the
   version may not.
4. If upstream's `pycrdt` cap moved, roll `requirements.txt`'s `pycrdt` pin
   forward to match (never pin backward — see the repo's roll-forward rule).
5. Run the suite: `python -m pytest`. A `SPEC_VERSION`/Yjs bump is expected to
   surface in `tests/test_ydoc_interchange.py` and `tests/test_wiki_bundle.py`
   (the version-gate tests and the whole-wiki round-trip).

[atd-v3-kahala]: https://github.com/VerinFast/good-place/blob/claude/atd-v3-kahala-rfc/docs/content/atd-v3-kahala.md
