# Last scanned for code smell: 2026-08-18 by Claude.
"""Reading a wiki bundle back: the streaming reader and the eager wrapper.

The consume half of :mod:`wiki_interchange.bundle` — see that module for the
archive layout and the security boundary. Everything here fails closed: the
manifest is version-gated and checked content-only *before* a page is touched,
member names are re-validated against traversal, and every image blob is
re-hashed against the digest it was stored under.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Any

from .bundle import (
    FORMAT_BUNDLE,
    MANIFEST_NAME,
    BundleElement,
    BundleImage,
    BundlePage,
    BundleTemplate,
    assert_bundle_content_only,
    image_member,
    page_member,
    validate_hash,
    validate_slug,
)
from .errors import MalformedEnvelopeError
from .snapshot import Snapshot
from .version import (
    WIKI_ENVELOPE_SPEC,
    YJS_SYNC_PROTOCOL_VERSION,
    ProtocolVersions,
    check_compatible,
)


@dataclass
class WikiBundle:
    """An unpacked wiki bundle: the version pair, label, pages and sections."""

    pages: list[BundlePage] = field(default_factory=list)
    elements: list[BundleElement] = field(default_factory=list)
    templates: list[BundleTemplate] = field(default_factory=list)
    images: list[BundleImage] = field(default_factory=list)
    label: str | None = None
    spec_version: int = WIKI_ENVELOPE_SPEC
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(self.spec_version, self.yjs_protocol)


def _section(manifest: dict[str, Any], key: str, id_key: str) -> list[dict[str, Any]]:
    """One manifest section, validated as a list of records carrying ``id_key``."""
    raw = manifest.get(key, [])
    if not isinstance(raw, list):
        raise MalformedEnvelopeError(f"bundle manifest {key!r} must be a list")
    for entry in raw:
        if not isinstance(entry, dict) or id_key not in entry:
            raise MalformedEnvelopeError(f"invalid bundle {key} entry: {entry!r}")
    return raw


class BundleReader:
    """Random-access reader over a bundle archive: one page in memory at a time.

    Opening validates the manifest — format tag, version gate, content-only, safe
    member names — so a hostile or incompatible bundle is refused before a single
    page is read. Blobs are re-hashed against the name they were stored under, so
    substituted bytes cannot land under a trusted digest.

    Use it as a context manager; :func:`unpack_bundle` is the eager wrapper.
    """

    def __init__(self, source: IO[bytes] | bytes):
        raw = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
        try:
            self._zip = zipfile.ZipFile(raw)
        except zipfile.BadZipFile as exc:
            raise MalformedEnvelopeError(f"bundle is not a valid archive: {exc}") from exc
        try:
            self.manifest = self._read_manifest()
        except Exception:
            self._zip.close()
            raise

    def _read_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self._zip.read(MANIFEST_NAME))
            fmt = manifest["format"]
            spec_version = int(manifest["spec_version"])
            yjs_protocol = int(manifest["yjs_protocol"])
            raw_pages = manifest["pages"]
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedEnvelopeError(f"invalid bundle manifest: {exc}") from exc
        if fmt != FORMAT_BUNDLE:
            raise MalformedEnvelopeError(f"unexpected bundle format tag: {fmt!r}")
        check_compatible(ProtocolVersions(spec_version, yjs_protocol))
        assert_bundle_content_only(manifest)
        if not isinstance(raw_pages, list):
            raise MalformedEnvelopeError("bundle manifest 'pages' must be a list")
        return manifest

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(
            int(self.manifest["spec_version"]), int(self.manifest["yjs_protocol"])
        )

    @property
    def label(self) -> str | None:
        return self.manifest.get("label")

    def elements(self) -> list[BundleElement]:
        """The wiki's custom element definitions (small; read whole)."""
        return [
            BundleElement(
                slug=e["slug"],
                name=e.get("name", ""),
                fields=list(e.get("fields") or []),
                html=e.get("html", ""),
                css=e.get("css", ""),
                js=e.get("js", ""),
            )
            for e in _section(self.manifest, "elements", "slug")
        ]

    def templates(self) -> list[BundleTemplate]:
        """The wiki's templates, each with the metadata schema it declares."""
        return [
            BundleTemplate(
                name=t["name"],
                markdown=t.get("markdown", ""),
                meta_schema=t.get("meta_schema", ""),
            )
            for t in _section(self.manifest, "templates", "name")
        ]

    def iter_images(self) -> Iterator[BundleImage]:
        """Yield the image index one blob at a time, each verified by hash.

        One blob in memory at a time — the point of the shared image area is that
        an importer can re-home each blob once and move on, without the whole
        wiki's binaries resident.
        """
        for entry in _section(self.manifest, "images", "sha256"):
            yield BundleImage(
                sha256=validate_hash(entry["sha256"]),
                media_type=entry.get("media_type", ""),
                image_ids=[str(i) for i in entry.get("image_ids") or []],
                filename=entry.get("filename"),
                byte_size=entry.get("byte_size"),
                data=self.blob(entry["sha256"]),
            )

    def images(self) -> list[BundleImage]:
        """The whole image index at once (blobs attached). Eager — see
        :meth:`iter_images` when the wiki is big."""
        return list(self.iter_images())

    def blob(self, sha256: str) -> bytes:
        """One image blob, re-hashed against the digest it is stored under."""
        digest = validate_hash(sha256)
        try:
            data = self._zip.read(image_member(digest))
        except KeyError as exc:
            raise MalformedEnvelopeError(f"bundle image {digest[:12]}… is missing") from exc
        actual = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual, digest):
            raise MalformedEnvelopeError(
                f"bundle image {digest[:12]}… does not match its content hash "
                f"(actual {actual[:12]}…)"
            )
        return data

    def iter_pages(self, *, attach_blobs: bool = True) -> Iterator[BundlePage]:
        """Yield every page, reading and validating one snapshot at a time.

        By default each page's sidecar refs get their hoisted blobs re-attached,
        so a caller that only knows about per-page snapshots sees exactly what it
        would have received had the page been shipped on its own.

        Pass ``attach_blobs=False`` when you are re-homing from
        :meth:`iter_images` instead: that is the whole point of the shared image
        area, and re-attaching would decompress the same blob once per page that
        embeds it — a wiki with a logo on two hundred pages would pay for it two
        hundred times.
        """
        for entry in self.manifest["pages"]:
            if not isinstance(entry, dict) or "slug" not in entry:
                raise MalformedEnvelopeError(f"invalid bundle page entry: {entry!r}")
            slug = validate_slug(entry["slug"])
            try:
                snapshot = self._zip.read(page_member(slug))
            except KeyError as exc:
                raise MalformedEnvelopeError(
                    f"bundle manifest lists page {slug!r} but its snapshot is missing"
                ) from exc
            snap = Snapshot.deserialize(snapshot)  # version gate on the page envelope
            # Fetch only the blobs *this* page names, so a wiki whose images
            # dwarf its prose still costs one page at a time.
            missing = (
                {img.sha256 for img in snap.images if img.data is None} if attach_blobs else set()
            )
            if missing:
                snapshot = snap.with_blobs({h: self.blob(h) for h in missing}).serialize()
            parent = entry.get("parent_slug")
            yield BundlePage(
                slug=slug,
                snapshot=snapshot,
                title=entry.get("title"),
                parent_slug=validate_slug(parent) if parent is not None else None,
                sort_order=entry.get("sort_order"),
                starred=bool(entry.get("starred", False)),
            )

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> BundleReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def unpack_bundle(raw: bytes | IO[bytes]) -> WikiBundle:
    """Parse a whole bundle, running the version + content-only + hash gates.

    Rejects (:class:`MalformedEnvelopeError`) a corrupt archive, a missing or
    malformed manifest, an unexpected format tag, a member name that could
    traverse the archive, a missing page or blob, or a blob whose bytes disagree
    with its digest; rejects
    (:class:`~wiki_interchange.errors.ServerFieldLeakError`) a manifest carrying a
    server-only field; and rejects
    (:class:`~wiki_interchange.errors.IncompatibleVersionError`) a bundle whose
    manifest or any page snapshot is version-incompatible. Never merges an
    unreadable bundle.

    Holds the whole wiki in memory; use :class:`BundleReader` for a big one.
    """
    with BundleReader(raw) as reader:
        return WikiBundle(
            pages=list(reader.iter_pages()),
            elements=reader.elements(),
            templates=reader.templates(),
            images=list(reader.iter_images()),
            label=reader.label,
            spec_version=reader.versions.spec_version,
            yjs_protocol=reader.versions.yjs_protocol,
        )
