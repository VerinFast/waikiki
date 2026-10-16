# Last scanned for code smell: 2026-08-05 by Claude.
"""Changelog: incremental Yjs sync — updates since a peer's state vector.

The standard Yjs sync handshake:

1. A peer publishes its **state vector** (:func:`state_vector`, ``doc.get_state()``)
   — a compact summary of everything it already has.
2. The other peer computes the **missing updates** since that vector
   (:func:`produce_changelog`, ``doc.get_update(state_vector)``) and ships them.
3. The first peer applies them (:func:`apply_changelog`).

Because CRDT updates are commutative and idempotent, running this both
directions converges the two docs with no lost writes — the same mechanism that
powers hosted real-time collaboration. This module carries the same version pair
and the same content-only guard as the snapshot layer.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from pycrdt import Doc

from .errors import MalformedEnvelopeError
from .schema import assert_content_only, assert_no_derived_data
from .version import (
    PAGE_ENVELOPE_SPEC,
    YJS_SYNC_PROTOCOL_VERSION,
    ProtocolVersions,
    check_compatible,
)

FORMAT_CHANGELOG = "good-place.wiki-interchange/changelog"


def state_vector(doc: Doc) -> bytes:
    """The peer's state vector — its summary of what it already holds."""
    return doc.get_state()


@dataclass
class Changelog:
    """An incremental-update envelope: version pair + the missing Yjs updates."""

    ydoc_update: bytes
    spec_version: int = PAGE_ENVELOPE_SPEC
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(self.spec_version, self.yjs_protocol)

    def serialize(self) -> bytes:
        """Encode the envelope as UTF-8 JSON (update payload base64-wrapped)."""
        envelope = {
            "format": FORMAT_CHANGELOG,
            "spec_version": self.spec_version,
            "yjs_protocol": self.yjs_protocol,
            "ydoc_update": base64.b64encode(self.ydoc_update).decode("ascii"),
        }
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> Changelog:
        """Parse an envelope and run the version/compat gate (reject on mismatch)."""
        try:
            envelope = json.loads(data)
            fmt = envelope["format"]
            log = cls(
                ydoc_update=base64.b64decode(envelope["ydoc_update"]),
                spec_version=int(envelope["spec_version"]),
                yjs_protocol=int(envelope["yjs_protocol"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedEnvelopeError(f"invalid changelog envelope: {exc}") from exc
        if fmt != FORMAT_CHANGELOG:
            raise MalformedEnvelopeError(f"unexpected changelog format tag: {fmt!r}")
        check_compatible(log.versions)
        return log


def produce_changelog(doc: Doc, since_state_vector: bytes) -> Changelog:
    """Produce the updates in ``doc`` that a peer at ``since_state_vector`` lacks.

    Fails closed if the doc carries server-only or derived fields.
    """
    assert_content_only(doc)
    assert_no_derived_data(doc)
    return Changelog(ydoc_update=doc.get_update(since_state_vector))


def apply_changelog(doc: Doc, source: bytes | Changelog) -> Doc:
    """Apply an incoming changelog to ``doc`` (in place) and return it.

    Runs the version gate, applies the update, then re-validates that the merged
    result carries nothing server-only.
    """
    log = source if isinstance(source, Changelog) else Changelog.deserialize(source)
    if isinstance(source, Changelog):
        check_compatible(log.versions)
    doc.apply_update(log.ydoc_update)
    assert_content_only(doc)
    return doc
