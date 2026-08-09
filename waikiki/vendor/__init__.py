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
WIKI_INTERCHANGE_VERSION = "0.1.0"

#: Upstream source coordinates, recorded so the vendored tree is re-syncable.
WIKI_INTERCHANGE_SOURCE = (
    "VerinFast/good-place:packages/wiki-interchange "
    "(branch claude/issue-3390-wiki-interchange)"
)
