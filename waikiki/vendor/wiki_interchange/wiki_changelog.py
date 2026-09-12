# Last scanned for code smell: 2026-09-02 by Claude.
"""Wiki-level changelog: incremental Yjs sync at the *whole-wiki* granularity.

The per-page ``changelog`` module ships one page's missing updates given a peer's
per-page state vector. This module lifts the same handshake to the wiki: a peer
publishes what it holds for **every page** at once, and the server responds with
per-page updates (for pages the peer already has) plus full snapshots (for pages
the peer is missing entirely). One round-trip re-synchronises a whole wiki that
has been out of contact — the incremental cousin of the whole-wiki snapshot
bundle (``bundle.py`` / ``bundle_reader.py``).

Compared to the bundle round-trip: the bundle ships **every page in full**, so a
re-sync of a 215-page wiki that only differs by a paragraph pushes tens of MB.
The wiki changelog ships **only the bytes the peer lacks**, so the same re-sync
is small — usually a few kilobytes.

Two envelopes travel together, one each direction:

* :class:`WikiStateVector` — the peer's per-page state-vector map for a whole
  wiki. Slugs are the portable identity (see the ``bundle`` module's note on why
  ids can't cross). Empty map = "I have nothing", so a fresh peer can bootstrap
  by pulling a full wiki changelog against an empty SV.
* :class:`WikiChangelog` — the response: for each page the peer already has,
  a per-page :class:`Changelog` update; for each page the peer lacks, a full
  :class:`Snapshot`. Also lists slugs the peer holds that the server does not
  (``missing_from_server``) so the peer can push them back.

Security boundary — identical to the per-page changelog + snapshot layers:

* **Content-only, tenant-neutral.** No tenant_id / wiki_id ever rides in the
  envelope. The target wiki is resolved from server context (Kahala's job on
  import), never the payload.
* **Version-gated.** Every envelope carries the spec + Yjs protocol version pair
  and is refused on mismatch — never merged blindly across incompatible peers.
* **Fail-closed.** Malformed envelopes raise
  :class:`~wiki_interchange.errors.MalformedEnvelopeError`; an incompatible pair
  raises :class:`~wiki_interchange.errors.IncompatibleVersionError`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from .bundle import BundleElement, BundleImage, BundleTemplate
from .errors import MalformedEnvelopeError
from .version import (
    PAGE_ENVELOPE_SPEC,
    WIKI_CHANGELOG_SPEC,
    YJS_SYNC_PROTOCOL_VERSION,
    ProtocolVersions,
    check_compatible,
)

FORMAT_WIKI_STATE_VECTOR = "good-place.wiki-interchange/wiki-state-vector"
FORMAT_WIKI_CHANGELOG = "good-place.wiki-interchange/wiki-changelog"


# --- Digests for the non-CRDT material ----------------------------------------
#
# Pages carry their own comparison mechanism: a Yjs state vector says precisely
# what a peer is missing. Elements and templates have no CRDT, so the peer has
# to say what it holds some other way, and the cheapest honest answer is a hash
# of the definition. Both sides MUST agree byte-for-byte on how that hash is
# computed or every sync would ship every definition forever -- which is exactly
# why these live here, in the shared library, and not in either peer.


def element_digest(element: BundleElement) -> str:
    """A stable content hash of a custom element's definition."""
    return _digest(
        [
            element.slug,
            element.name,
            element.html,
            element.css,
            element.js,
            json.dumps(element.fields, sort_keys=True, separators=(",", ":")),
        ]
    )


def template_digest(template: BundleTemplate) -> str:
    """A stable content hash of a template, metadata schema included."""
    return _digest([template.name, template.markdown, template.meta_schema])


def _digest(parts: list[str]) -> str:
    """SHA-256 over length-prefixed parts.

    Length-prefixed rather than joined by a separator: a separator can appear
    inside a definition (an element's CSS very much contains newlines), so
    joining would let two different definitions hash identically and one of them
    would silently never sync.
    """
    h = hashlib.sha256()
    for part in parts:
        raw = (part or "").encode("utf-8")
        h.update(str(len(raw)).encode("ascii"))
        h.update(b":")
        h.update(raw)
    return h.hexdigest()


# --- Wiki state vector --------------------------------------------------------


@dataclass
class WikiStateVector:
    """A peer's per-page state-vector map for a whole wiki.

    ``pages`` maps a page's slug to its Yjs :func:`state_vector` bytes — the
    compact summary that lets the other peer compute only the missing updates.
    A slug that is absent means "I have never seen this page" (the peer needs a
    full snapshot for it). An empty map is a valid bootstrap request.

    The v3 sections say what **non-CRDT** material the sender already holds, so
    the responder can send only the difference rather than every definition
    every time:

    * ``elements`` — element slug to :func:`element_digest`
    * ``templates`` — template name to :func:`template_digest`
    * ``images`` — the content hashes (sha256) of image blobs held

    All three are hints. A peer on an older build omits them, which reads as "I
    hold nothing", and the responder then sends everything it has — wasteful for
    one exchange, never wrong.
    """

    pages: dict[str, bytes] = field(default_factory=dict)
    elements: dict[str, str] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    images: list[str] = field(default_factory=list)
    spec_version: int = PAGE_ENVELOPE_SPEC
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(self.spec_version, self.yjs_protocol)

    def serialize(self) -> bytes:
        """Encode the envelope as UTF-8 JSON (per-page SVs base64-wrapped).

        The spec stamp stays at the page floor even when the v3 sections are
        populated: they are a hint about what the sender holds, and a peer that
        ignores them answers correctly anyway. See ``WIKI_CHANGELOG_SPEC``.
        """
        envelope = {
            "format": FORMAT_WIKI_STATE_VECTOR,
            "spec_version": self.spec_version,
            "yjs_protocol": self.yjs_protocol,
            "pages": {
                slug: base64.b64encode(sv).decode("ascii")
                for slug, sv in sorted(self.pages.items())
            },
            "elements": dict(sorted(self.elements.items())),
            "templates": dict(sorted(self.templates.items())),
            "images": sorted(set(self.images)),
        }
        return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> WikiStateVector:
        """Parse a wiki state-vector envelope and run the version gate."""
        try:
            envelope = json.loads(data)
            fmt = envelope["format"]
            spec_version = int(envelope["spec_version"])
            yjs_protocol = int(envelope["yjs_protocol"])
            raw_pages = envelope.get("pages", {})
            raw_elements = envelope.get("elements", {}) or {}
            raw_templates = envelope.get("templates", {}) or {}
            raw_images = envelope.get("images", []) or []
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedEnvelopeError(f"invalid wiki state-vector envelope: {exc}") from exc
        if fmt != FORMAT_WIKI_STATE_VECTOR:
            raise MalformedEnvelopeError(f"unexpected wiki state-vector format tag: {fmt!r}")
        if not isinstance(raw_pages, Mapping):
            raise MalformedEnvelopeError(
                "wiki state-vector 'pages' must be an object (slug -> base64 SV)"
            )
        try:
            pages = {str(slug): base64.b64decode(sv) for slug, sv in raw_pages.items()}
        except (ValueError, TypeError) as exc:
            raise MalformedEnvelopeError(
                f"invalid per-page state vector in wiki envelope: {exc}"
            ) from exc
        for name, value in (("elements", raw_elements), ("templates", raw_templates)):
            if not isinstance(value, Mapping):
                raise MalformedEnvelopeError(
                    f"wiki state-vector {name!r} must be an object (name -> digest)"
                )
        if not isinstance(raw_images, list):
            raise MalformedEnvelopeError("wiki state-vector 'images' must be a list")
        sv = cls(
            pages=pages,
            elements={str(k): str(v) for k, v in raw_elements.items()},
            templates={str(k): str(v) for k, v in raw_templates.items()},
            images=[str(h) for h in raw_images],
            spec_version=spec_version,
            yjs_protocol=yjs_protocol,
        )
        check_compatible(sv.versions)
        return sv


# --- Wiki changelog -----------------------------------------------------------


@dataclass(frozen=True)
class WikiChangelogPage:
    """One page's contribution to a wiki changelog.

    Exactly one of ``changelog`` (raw serialized :class:`Changelog` bytes — the
    envelope, not just the ydoc_update) or ``snapshot`` (raw serialized
    :class:`Snapshot` bytes) is populated:

    * ``changelog`` — the peer already has this page; ship only the missing
      updates. The bytes are the per-page changelog envelope so its version pair
      travels inline and its own gate fires on apply.
    * ``snapshot`` — the peer does not have this page yet; ship a full
      per-page snapshot. Same shape the per-page snapshot round-trip uses.

    ``title`` is a convenience hint the human export path already carries; the
    canonical title lives in the doc itself.
    """

    slug: str
    changelog: bytes | None = None
    snapshot: bytes | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        if (self.changelog is None) == (self.snapshot is None):
            raise MalformedEnvelopeError(
                f"wiki-changelog page {self.slug!r}: exactly one of "
                "'changelog' or 'snapshot' must be set"
            )


@dataclass
class WikiChangelog:
    """The server's response to a peer's :class:`WikiStateVector`.

    ``pages`` carries one :class:`WikiChangelogPage` per server page: a
    changelog (updates the peer lacks) for pages the peer knew about, and a
    snapshot for pages the peer had never seen. ``missing_from_server`` names
    slugs the peer's SV mentioned that the server does not have — a hint that
    the peer can push those separately (per-page or via a wiki-level push).

    ``elements``, ``templates`` and ``images`` (spec v3) carry the **non-CRDT**
    material the peer's state vector said it lacks or holds at a different
    digest. Before v3 they did not travel at all, so a peer syncing
    incrementally kept its pages current while its definitions silently went
    stale — pages rendering against an element the peer had never seen, with
    nothing anywhere reporting a problem. Image blobs carry their bytes in
    ``BundleImage.data``; the peer re-homes them and rewrites its own
    ``/image/<id>`` references, exactly as it does for a bundle.

    ``spec_version`` is computed rather than fixed: an envelope carrying only
    pages is shaped exactly as it always was and stamps the page floor, so an
    older peer can still read it. One carrying definitions stamps
    :data:`WIKI_CHANGELOG_SPEC`, so an older peer **rejects it** instead of
    parsing it happily and dropping the sections it does not know about.
    """

    pages: list[WikiChangelogPage] = field(default_factory=list)
    missing_from_server: list[str] = field(default_factory=list)
    elements: list[BundleElement] = field(default_factory=list)
    templates: list[BundleTemplate] = field(default_factory=list)
    images: list[BundleImage] = field(default_factory=list)
    spec_version: int | None = None
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.spec_version is None:
            self.spec_version = (
                WIKI_CHANGELOG_SPEC if self.carries_definitions else PAGE_ENVELOPE_SPEC
            )

    @property
    def carries_definitions(self) -> bool:
        """Whether this envelope holds anything only a v3 peer can read."""
        return bool(self.elements or self.templates or self.images)

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(self.spec_version, self.yjs_protocol)

    def serialize(self) -> bytes:
        """Encode the wiki changelog envelope (per-page payloads base64-wrapped)."""
        pages: list[dict] = []
        for p in sorted(self.pages, key=lambda x: x.slug):
            entry: dict = {"slug": p.slug}
            if p.title is not None:
                entry["title"] = p.title
            if p.changelog is not None:
                entry["changelog"] = base64.b64encode(p.changelog).decode("ascii")
            if p.snapshot is not None:
                entry["snapshot"] = base64.b64encode(p.snapshot).decode("ascii")
            pages.append(entry)
        envelope = {
            "format": FORMAT_WIKI_CHANGELOG,
            "spec_version": self.spec_version,
            "yjs_protocol": self.yjs_protocol,
            "pages": pages,
            "missing_from_server": sorted(self.missing_from_server),
            "elements": [
                {
                    "slug": e.slug,
                    "name": e.name,
                    "fields": e.fields,
                    "html": e.html,
                    "css": e.css,
                    "js": e.js,
                }
                for e in sorted(self.elements, key=lambda e: e.slug)
            ],
            "templates": [
                {"name": t.name, "markdown": t.markdown, "meta_schema": t.meta_schema}
                for t in sorted(self.templates, key=lambda t: t.name)
            ],
            "images": [
                {
                    "sha256": i.sha256,
                    "media_type": i.media_type,
                    "image_ids": list(i.image_ids),
                    "filename": i.filename,
                    "byte_size": i.byte_size,
                    "data": base64.b64encode(i.data).decode("ascii") if i.data else None,
                }
                for i in sorted(self.images, key=lambda i: i.sha256)
            ],
        }
        return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> WikiChangelog:
        """Parse a wiki changelog envelope and run the version gate."""
        try:
            envelope = json.loads(data)
            fmt = envelope["format"]
            spec_version = int(envelope["spec_version"])
            yjs_protocol = int(envelope["yjs_protocol"])
            raw_pages = envelope.get("pages", [])
            missing = envelope.get("missing_from_server", [])
            raw_elements = envelope.get("elements", []) or []
            raw_templates = envelope.get("templates", []) or []
            raw_images = envelope.get("images", []) or []
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedEnvelopeError(f"invalid wiki changelog envelope: {exc}") from exc
        if fmt != FORMAT_WIKI_CHANGELOG:
            raise MalformedEnvelopeError(f"unexpected wiki changelog format tag: {fmt!r}")
        if not isinstance(raw_pages, list):
            raise MalformedEnvelopeError("wiki changelog 'pages' must be a list")
        if not isinstance(missing, list):
            raise MalformedEnvelopeError("wiki changelog 'missing_from_server' must be a list")
        pages: list[WikiChangelogPage] = []
        for entry in raw_pages:
            if not isinstance(entry, dict) or "slug" not in entry:
                raise MalformedEnvelopeError(f"invalid wiki changelog page entry: {entry!r}")
            try:
                changelog_b64 = entry.get("changelog")
                snapshot_b64 = entry.get("snapshot")
                pages.append(
                    WikiChangelogPage(
                        slug=str(entry["slug"]),
                        changelog=(
                            base64.b64decode(changelog_b64) if changelog_b64 is not None else None
                        ),
                        snapshot=(
                            base64.b64decode(snapshot_b64) if snapshot_b64 is not None else None
                        ),
                        title=entry.get("title"),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise MalformedEnvelopeError(
                    f"invalid wiki changelog page payload for {entry.get('slug')!r}: {exc}"
                ) from exc
        for name, value in (
            ("elements", raw_elements),
            ("templates", raw_templates),
            ("images", raw_images),
        ):
            if not isinstance(value, list):
                raise MalformedEnvelopeError(f"wiki changelog {name!r} must be a list")
        try:
            elements = [
                BundleElement(
                    slug=str(e["slug"]),
                    name=str(e.get("name") or e["slug"]),
                    fields=list(e.get("fields") or []),
                    html=str(e.get("html") or ""),
                    css=str(e.get("css") or ""),
                    js=str(e.get("js") or ""),
                )
                for e in raw_elements
            ]
            templates = [
                BundleTemplate(
                    name=str(t["name"]),
                    markdown=str(t.get("markdown") or ""),
                    meta_schema=str(t.get("meta_schema") or ""),
                )
                for t in raw_templates
            ]
            images = [
                BundleImage(
                    sha256=str(i["sha256"]),
                    media_type=str(i.get("media_type") or "application/octet-stream"),
                    image_ids=[str(x) for x in (i.get("image_ids") or [])],
                    filename=i.get("filename"),
                    byte_size=i.get("byte_size"),
                    data=base64.b64decode(i["data"]) if i.get("data") else None,
                )
                for i in raw_images
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedEnvelopeError(
                f"invalid wiki changelog definition section: {exc}"
            ) from exc
        log = cls(
            pages=pages,
            missing_from_server=[str(s) for s in missing],
            elements=elements,
            templates=templates,
            images=images,
            spec_version=spec_version,
            yjs_protocol=yjs_protocol,
        )
        check_compatible(log.versions)
        return log
