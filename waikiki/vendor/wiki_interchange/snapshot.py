# Last scanned for code smell: 2026-08-05 by Claude.
"""Snapshot encoder/decoder: full Y.Doc state + a non-CRDT image sidecar.

A **snapshot** is the whole wiki page at a point in time:

* ``ydoc_state`` — the full encoded Y.Doc update (``doc.get_update()``, pycrdt's
  equivalent of ``Y.encodeStateAsUpdate``); applying it to a fresh ``Doc``
  reconstructs the page exactly.
* ``images`` — a non-CRDT sidecar of image references. Images are binary blobs
  that do not belong inside the CRDT; they ride alongside as content-addressed
  refs (or optional inline blobs).

Deliberately **excluded**: derived data (RAG chunks / embeddings). It is never
shipped — regenerated on import — so model/dimension drift cannot travel. The
encoder enforces this via :func:`~wiki_interchange.schema.assert_no_derived_data`.

Security boundary: an :class:`ImageRef` carries only content-level fields — a
content hash, media type, filename, optional inline bytes. It intentionally has
**no** ``tenant_id`` / ``wiki_id`` / bucket-path field, so a server GCS URI that
encodes a tenant can never leak through the sidecar. Re-homing an image under
the importing tenant's prefix is the server's job (K3), not the format's.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from pycrdt import Doc

from .errors import MalformedEnvelopeError, ServerFieldLeakError
from .schema import (
    SERVER_ONLY_FIELD_NAMES,
    assert_content_only,
    assert_no_derived_data,
)
from .version import (
    SPEC_VERSION,
    YJS_SYNC_PROTOCOL_VERSION,
    ProtocolVersions,
    check_compatible,
)

FORMAT_SNAPSHOT = "good-place.wiki-interchange/snapshot"


@dataclass(frozen=True)
class ImageRef:
    """A tenant-neutral reference to one image in the sidecar.

    ``data`` (inline blob) is optional; when omitted, the importing side fetches
    or re-homes the blob by ``sha256``. No server path/bucket/tenant field
    exists here by design.
    """

    image_id: str
    media_type: str
    sha256: str
    filename: str | None = None
    byte_size: int | None = None
    data: bytes | None = None

    def _to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "image_id": self.image_id,
            "media_type": self.media_type,
            "sha256": self.sha256,
        }
        if self.filename is not None:
            out["filename"] = self.filename
        if self.byte_size is not None:
            out["byte_size"] = self.byte_size
        if self.data is not None:
            out["data"] = base64.b64encode(self.data).decode("ascii")
        return out

    @classmethod
    def _from_json(cls, obj: dict[str, Any]) -> ImageRef:
        raw = obj.get("data")
        return cls(
            image_id=obj["image_id"],
            media_type=obj["media_type"],
            sha256=obj["sha256"],
            filename=obj.get("filename"),
            byte_size=obj.get("byte_size"),
            data=base64.b64decode(raw) if raw is not None else None,
        )


@dataclass
class Snapshot:
    """A full-page snapshot envelope: version pair + Y.Doc state + image sidecar."""

    ydoc_state: bytes
    images: list[ImageRef] = field(default_factory=list)
    spec_version: int = SPEC_VERSION
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(self.spec_version, self.yjs_protocol)

    def serialize(self) -> bytes:
        """Encode the envelope as UTF-8 JSON (binary payloads base64-wrapped)."""
        envelope = {
            "format": FORMAT_SNAPSHOT,
            "spec_version": self.spec_version,
            "yjs_protocol": self.yjs_protocol,
            "ydoc_state": base64.b64encode(self.ydoc_state).decode("ascii"),
            "images": [img._to_json() for img in self.images],
        }
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> Snapshot:
        """Parse an envelope and run the version/compat gate (reject on mismatch)."""
        try:
            envelope = json.loads(data)
            fmt = envelope["format"]
            snap = cls(
                ydoc_state=base64.b64decode(envelope["ydoc_state"]),
                images=[ImageRef._from_json(i) for i in envelope.get("images", [])],
                spec_version=int(envelope["spec_version"]),
                yjs_protocol=int(envelope["yjs_protocol"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedEnvelopeError(f"invalid snapshot envelope: {exc}") from exc
        if fmt != FORMAT_SNAPSHOT:
            raise MalformedEnvelopeError(f"unexpected snapshot format tag: {fmt!r}")
        check_compatible(snap.versions)
        return snap


def _assert_images_content_only(images: list[ImageRef]) -> None:
    for img in images:
        leaked = {k for k in asdict(img) if k.casefold() in SERVER_ONLY_FIELD_NAMES}
        if leaked:
            raise ServerFieldLeakError(
                f"image sidecar carries server-only fields: {sorted(leaked)}"
            )


def encode_snapshot(doc: Doc, images: list[ImageRef] | None = None) -> Snapshot:
    """Encode ``doc`` (+ optional image sidecar) into a :class:`Snapshot`.

    Fails closed if the doc carries server-only identity/authz fields or derived
    RAG/embedding data.
    """
    assert_content_only(doc)
    assert_no_derived_data(doc)
    images = list(images or [])
    _assert_images_content_only(images)
    return Snapshot(ydoc_state=doc.get_update(), images=images)


def decode_snapshot(source: bytes | Snapshot, into: Doc | None = None) -> Doc:
    """Reconstruct a :class:`~pycrdt.Doc` from a snapshot.

    Runs the version gate (via :meth:`Snapshot.deserialize`), applies the encoded
    state, then re-validates that nothing server-only rode in. Returns the doc.
    """
    snap = source if isinstance(source, Snapshot) else Snapshot.deserialize(source)
    if isinstance(source, Snapshot):
        check_compatible(snap.versions)
    doc = into if into is not None else Doc()
    doc.apply_update(snap.ydoc_state)
    assert_content_only(doc)
    return doc
