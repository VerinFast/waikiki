"""Vendored third-party packages, version-pinned and re-syncable.

Waikiki ships as a standalone desktop app, so packages that are not published to
PyPI are vendored here rather than declared as dependencies. Each subpackage is a
verbatim copy of an upstream source tree at a recorded revision; see
``docs/vendoring.md`` for the source repo/branch, the pinned version, and the
re-sync steps.

Currently vendored:

* ``wiki_interchange`` — the shared, content-only Y.Doc interchange format for the
  Kahala <-> Waikiki round-trip (good-place ``packages/wiki-interchange``). Pinned
  at :data:`WIKI_INTERCHANGE_VERSION`.
"""
from __future__ import annotations

#: Pinned version of the vendored ``wiki_interchange`` package (its
#: ``__version__``). Keep this in lockstep with ``docs/vendoring.md`` and
#: ``requirements.txt`` whenever the tree under ``vendor/wiki_interchange`` is
#: re-synced from upstream.
WIKI_INTERCHANGE_VERSION = "0.2.0"

#: Upstream commit the tree is byte-identical to. **This, not the version, is
#: the real pin.** Upstream shipped the whole-wiki changelog (a new module and
#: five new exports) without moving ``__version__`` off ``0.2.0``, so the
#: version string cannot tell you the vendored copy is stale -- which is exactly
#: how it went stale. Compare revisions when checking, and move this on every
#: re-sync even when the version has not changed.
WIKI_INTERCHANGE_REVISION = "46bbf8c9"

#: Upstream source coordinates, recorded so the vendored tree is re-syncable.
#: GitLab is the canonical remote for the monorepo; the GitHub repo mirrors the
#: same code but issues and MRs live on GitLab.
WIKI_INTERCHANGE_SOURCE = (
    "gitlab.kwirker.com/good-place/platform:packages/wiki-interchange "
    "(branch main, MR good-place/platform!85 — K6b whole-wiki changelog)"
)
