# Last scanned for code smell: 2026-08-05 by Claude.
"""wiki-interchange — the shared Y.Doc interchange library for atd_v3 / Kahala.

Content-only, tenant-neutral round-trip format for wiki pages, built on pycrdt
(Yjs/Yrs bindings). Consumed by the in-monorepo ``services/kahala`` and vendored
+ version-pinned by the external ``VerinFast/waikiki`` app; the two stay in
lockstep through the version/compat gate in :mod:`wiki_interchange.version`.

Public surface:

* schema — :func:`build_page_doc`, the root-type constants, and the content-only
  / no-derived-data guards.
* snapshot — :func:`encode_snapshot` / :func:`decode_snapshot`, :class:`Snapshot`,
  :class:`ImageRef`.
* changelog — :func:`state_vector`, :func:`produce_changelog`,
  :func:`apply_changelog`, :class:`Changelog`.
* bundle — :func:`pack_bundle` / :func:`pack_bundle_into` / :func:`unpack_bundle`
  / :class:`BundleReader`, :class:`WikiBundle`, :class:`BundlePage`,
  :class:`BundleElement`, :class:`BundleTemplate`, :class:`BundleImage`: a whole
  wiki — pages with their hierarchy, order and starred flags, custom elements,
  templates (with metadata schemas), and one copy of each image blob.
* version — :data:`SPEC_VERSION`, :data:`YJS_SYNC_PROTOCOL_VERSION`,
  :func:`negotiate`, :func:`check_compatible`, :class:`Compatibility`.
"""

from __future__ import annotations

from .bundle import (
    FORMAT_BUNDLE,
    BundleElement,
    BundleImage,
    BundlePage,
    BundleTemplate,
    assert_bundle_content_only,
    pack_bundle,
    pack_bundle_into,
)
from .bundle_reader import BundleReader, WikiBundle, unpack_bundle
from .changelog import (
    FORMAT_CHANGELOG,
    Changelog,
    apply_changelog,
    produce_changelog,
    state_vector,
)
from .errors import (
    DerivedDataError,
    IncompatibleVersionError,
    InterchangeError,
    MalformedEnvelopeError,
    ServerFieldLeakError,
)
from .schema import (
    DERIVED_FIELD_NAMES,
    ROOT_COMMENTS,
    ROOT_CONTENT,
    ROOT_ELEMENTS,
    ROOT_META,
    ROOT_TAGS,
    ROOT_TREE,
    SERVER_ONLY_FIELD_NAMES,
    assert_content_only,
    assert_no_derived_data,
    build_page_doc,
    collect_field_names,
)
from .snapshot import (
    FORMAT_SNAPSHOT,
    ImageRef,
    Snapshot,
    decode_snapshot,
    encode_snapshot,
)
from .version import (
    MIN_COMPATIBLE_SPEC_VERSION,
    PAGE_ENVELOPE_SPEC,
    SPEC_VERSION,
    WIKI_ENVELOPE_SPEC,
    YJS_SYNC_PROTOCOL_VERSION,
    Compatibility,
    ProtocolVersions,
    check_compatible,
    negotiate,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # version / compat gate
    "SPEC_VERSION",
    "MIN_COMPATIBLE_SPEC_VERSION",
    "PAGE_ENVELOPE_SPEC",
    "WIKI_ENVELOPE_SPEC",
    "YJS_SYNC_PROTOCOL_VERSION",
    "Compatibility",
    "ProtocolVersions",
    "negotiate",
    "check_compatible",
    # schema
    "build_page_doc",
    "collect_field_names",
    "assert_content_only",
    "assert_no_derived_data",
    "SERVER_ONLY_FIELD_NAMES",
    "DERIVED_FIELD_NAMES",
    "ROOT_CONTENT",
    "ROOT_TREE",
    "ROOT_COMMENTS",
    "ROOT_TAGS",
    "ROOT_ELEMENTS",
    "ROOT_META",
    # snapshot
    "encode_snapshot",
    "decode_snapshot",
    "Snapshot",
    "ImageRef",
    "FORMAT_SNAPSHOT",
    # changelog
    "state_vector",
    "produce_changelog",
    "apply_changelog",
    "Changelog",
    "FORMAT_CHANGELOG",
    # bundle (whole-wiki container)
    "pack_bundle",
    "pack_bundle_into",
    "unpack_bundle",
    "assert_bundle_content_only",
    "BundlePage",
    "BundleElement",
    "BundleTemplate",
    "BundleImage",
    "BundleReader",
    "WikiBundle",
    "FORMAT_BUNDLE",
    # errors
    "InterchangeError",
    "IncompatibleVersionError",
    "ServerFieldLeakError",
    "DerivedDataError",
    "MalformedEnvelopeError",
]
