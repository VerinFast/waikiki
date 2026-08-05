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
| **Pinned version** | `0.1.0` (spec `SPEC_VERSION = 1`, Yjs sync protocol `1`) |
| **Upstream** | `VerinFast/good-place`, `packages/wiki-interchange/wiki_interchange/` |
| **Upstream branch** | `claude/issue-3390-wiki-interchange` (W1, PR good-place#3409) |
| **Runtime dep** | `pycrdt>=0.10,<0.15` (satisfied by Waikiki's own pin) |

### What it is

`wiki_interchange` is the **shared, content-only Y.Doc interchange format** for
the Kahala ⟷ Waikiki round-trip (RFC [atd-v3-kahala], decision D3: the format is
a small library both sides vendor + version-pin, not a service and not on PyPI).
Waikiki consumes it through `waikiki/ydoc.py`:

- `build_page_doc` / the root-type constants — the canonical page Y.Doc layout
  (`content` Text, `tree`/`comments`/`tags`/`elements` Arrays, `meta` Map).
- `encode_snapshot` / `decode_snapshot` — full Y.Doc + content-addressed
  `ImageRef` sidecar (**produce**/**consume**).
- `state_vector` / `produce_changelog` / `apply_changelog` — incremental sync.
- `check_compatible` / `negotiate` — the spec + Yjs-protocol version gate that
  **rejects** an incompatible envelope rather than merging bad bytes.

The format carries **content only** — no `tenant_id` / `wiki_id` / permissions
(server-owned, re-attached by Kahala) and no RAG chunks/embeddings (derived,
regenerated locally on import). The vendored guards enforce this on both encode
and decode.

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
   surface in `tests/test_ydoc_interchange.py` (the version-gate tests).

[atd-v3-kahala]: https://github.com/VerinFast/good-place/blob/claude/atd-v3-kahala-rfc/docs/content/atd-v3-kahala.md
