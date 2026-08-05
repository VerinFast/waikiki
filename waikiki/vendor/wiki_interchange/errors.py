# Last scanned for code smell: 2026-08-05 by Claude.
"""Exception hierarchy for the wiki-interchange format layer.

Every error a peer can raise while producing or consuming a snapshot or a
changelog descends from :class:`InterchangeError`, so a caller can catch the
whole family at the round-trip boundary.
"""

from __future__ import annotations


class InterchangeError(Exception):
    """Base class for every wiki-interchange format error."""


class IncompatibleVersionError(InterchangeError):
    """Two peers cannot exchange data because their spec/Yjs versions disagree.

    Raised by the version/compat gate on the *reject* path (a newer-than-known
    spec, or a mismatched Yjs sync protocol). This is the lockstep guard that
    keeps the in-monorepo Kahala and the external Waikiki from silently merging
    incompatible bytes.
    """


class ServerFieldLeakError(InterchangeError):
    """A doc/envelope carried a server-only identity/authz field.

    The interchange format is *content only*. ``tenant_id``, ``wiki_id``,
    membership and permissions belong to the server and must never appear in a
    snapshot or changelog. If they do, encoding fails closed rather than ship a
    payload that a local import could use to escalate tenancy.
    """


class DerivedDataError(InterchangeError):
    """A doc carried derived data (RAG chunks / embeddings) that must be excluded.

    Derived vectors are regenerated on import (via the broker on Kahala, a local
    embedder on Waikiki) so model/dimension drift can never travel with content.
    """


class MalformedEnvelopeError(InterchangeError):
    """A serialized snapshot/changelog envelope was missing fields or malformed."""
