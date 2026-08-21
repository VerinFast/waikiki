# Vendored packages

Waikiki ships as a standalone desktop app, so a dependency that is **not
published to PyPI** is vendored into the source tree rather than declared in
`requirements.txt`. Each vendored tree is a verbatim copy of an upstream package
at a recorded revision, with the pinned version recorded in three places that
must stay in lockstep:

- `waikiki/vendor/__init__.py` — `WIKI_INTERCHANGE_VERSION` / `_SOURCE`
- `requirements.txt` — the "VENDORED, not on PyPI" note
- this document

## `wiki_interchange`

| | |
|---|---|
| **Path** | `waikiki/vendor/wiki_interchange/` |
| **Pinned version** | `0.2.0` (spec `SPEC_VERSION = 2`, floor `1`, Yjs sync protocol `1`) |
| **Upstream** | `VerinFast/good-place`, `packages/wiki-interchange/wiki_interchange/` (**private repo** — first-party, same owner; the vendored copy here is the public one, under this project's Elastic License 2.0) |
| **Upstream branch** | `dev` (PR good-place#3683, merged — it subsumed #3677) |
| **Upstream revision** | `9ea72c8c` — the vendored tree is byte-identical to `packages/wiki-interchange/wiki_interchange/` at this commit |
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
- `state_vector` / `produce_changelog` / `apply_changelog` — incremental sync.
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

### Import atomicity (known limit)

`store.import_wiki_bundle` decodes the **whole** payload before its first write
(`store._read_bundle`), so a malformed, version-incompatible or tampered bundle —
the realistic failure mode, since it arrives from a peer — leaves the wiki
untouched. A failure of the local *writes* (full disk, lock) can still leave a
partial import; making that atomic too means staging into a scratch database and
swapping the file, which is deliberately out of scope for issue #57.

### Why vendored (not a dependency)

The desktop app bundles its own Python; there is no package index at install
time, and the library must stay byte-for-byte in lockstep with the in-monorepo
Kahala service through the version gate. Vendoring makes the pin explicit and the
build self-contained.

### Re-sync steps

When W1 (or a later spec bump) changes upstream:

1. Pull the package tree from the recorded branch (or its merge on `main`):

   ```sh
   gh api "repos/VerinFast/good-place/contents/packages/wiki-interchange/\
   wiki_interchange?ref=<branch>" --jq '.[].path'
   # fetch each file's raw content into waikiki/vendor/wiki_interchange/
   ```

2. Copy the `wiki_interchange/` package over `waikiki/vendor/wiki_interchange/`
   (keep only the package — not its `pyproject.toml`/`tests/`, which stay
   upstream).
3. Read the new `__version__` from upstream's `wiki_interchange/__init__.py` and
   update the pin in **all three** places listed at the top of this file.
4. If upstream's `pycrdt` cap moved, roll `requirements.txt`'s `pycrdt` pin
   forward to match (never pin backward — see the repo's roll-forward rule).
5. Run the suite: `python -m pytest`. A `SPEC_VERSION`/Yjs bump is expected to
   surface in `tests/test_ydoc_interchange.py` and `tests/test_wiki_bundle.py`
   (the version-gate tests and the whole-wiki round-trip).

[atd-v3-kahala]: https://github.com/VerinFast/good-place/blob/claude/atd-v3-kahala-rfc/docs/content/atd-v3-kahala.md
