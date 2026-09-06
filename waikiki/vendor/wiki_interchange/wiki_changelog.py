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
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import MalformedEnvelopeError
from .version import (
    PAGE_ENVELOPE_SPEC,
    YJS_SYNC_PROTOCOL_VERSION,
    ProtocolVersions,
    check_compatible,
)

FORMAT_WIKI_STATE_VECTOR = "good-place.wiki-interchange/wiki-state-vector"
FORMAT_WIKI_CHANGELOG = "good-place.wiki-interchange/wiki-changelog"


# --- Wiki state vector --------------------------------------------------------


@dataclass
class WikiStateVector:
    """A peer's per-page state-vector map for a whole wiki.

    ``pages`` maps a page's slug to its Yjs :func:`state_vector` bytes — the
    compact summary that lets the other peer compute only the missing updates.
    A slug that is absent means "I have never seen this page" (the peer needs a
    full snapshot for it). An empty map is a valid bootstrap request.
    """

    pages: dict[str, bytes] = field(default_factory=dict)
    spec_version: int = PAGE_ENVELOPE_SPEC
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    @property
    def versions(self) -> ProtocolVersions:
        return ProtocolVersions(self.spec_version, self.yjs_protocol)

    def serialize(self) -> bytes:
        """Encode the envelope as UTF-8 JSON (per-page SVs base64-wrapped)."""
        envelope = {
            "format": FORMAT_WIKI_STATE_VECTOR,
            "spec_version": self.spec_version,
            "yjs_protocol": self.yjs_protocol,
            "pages": {
                slug: base64.b64encode(sv).decode("ascii")
                for slug, sv in sorted(self.pages.items())
            },
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
        sv = cls(pages=pages, spec_version=spec_version, yjs_protocol=yjs_protocol)
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
    """

    pages: list[WikiChangelogPage] = field(default_factory=list)
    missing_from_server: list[str] = field(default_factory=list)
    spec_version: int = PAGE_ENVELOPE_SPEC
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

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
        log = cls(
            pages=pages,
            missing_from_server=[str(s) for s in missing],
            spec_version=spec_version,
            yjs_protocol=yjs_protocol,
        )
        check_compatible(log.versions)
        return log
