# Last scanned for code smell: 2026-08-05 by Claude.
"""Spec + Yjs sync protocol versions and the compatibility gate.

Two independent version numbers travel in every snapshot/changelog/bundle
envelope:

* ``SPEC_VERSION`` — the good-place wiki-interchange *spec* version. Bump it when
  the envelope shape or the Y.Doc shared-type layout changes in a way peers must
  agree on. What an envelope *stamps* is the floor for its own kind
  (``PAGE_ENVELOPE_SPEC`` / ``WIKI_ENVELOPE_SPEC``), not this number — see the
  note beside those constants.
* ``YJS_SYNC_PROTOCOL_VERSION`` — the Yjs update/sync *binary* protocol version.
  pycrdt / yrs emit the Yjs v1 update format; two peers MUST share this exactly
  or the CRDT bytes are not safely mergeable. This is independent of the spec.

The gate below is the lockstep mechanism between the in-monorepo Kahala service
and the externally-vendored Waikiki app: peers negotiate on these numbers and
**reject or upgrade on mismatch** rather than merge blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import IncompatibleVersionError

SPEC_VERSION = 3
"""Highest wiki-interchange spec version this build understands.

v2 added the wiki-level bundle's content sections — page hierarchy by slug, sort
order, starred, custom elements, templates with their metadata schema, and a
shared content-addressed image area.

v3 gives the **wiki changelog** those same sections. Until v3 the incremental
path carried pages and nothing else, so a peer that synced incrementally could
never learn about a new image, a changed custom element or a new template: its
pages would render against definitions it did not have, and nothing would say
so. Both steps are strictly **additive** — the page Y.Doc root layout and the
per-page snapshot/changelog envelopes are unchanged from v1.
"""

MIN_COMPATIBLE_SPEC_VERSION = 1
"""Oldest spec version this build can still read (the upgrade-on-import floor).

Stays **1**. Because v2 only added material, a v2 build reads a v1 payload with
no translation: every v1 field means exactly what it meant, and the v2 sections
are simply absent (an unordered, unstarred, flat wiki with no elements or
templates — which is what a v1 bundle actually described). The floor rises only
when we lose the ability to read an old payload, which is not the case here.
"""

YJS_SYNC_PROTOCOL_VERSION = 1
"""Yjs update binary protocol version. Must match exactly between peers."""

# --- What each envelope kind stamps -------------------------------------------
# An envelope declares the **oldest spec that can read it**, not the version of
# the build that produced it. The two diverge as soon as the spec grows a feature
# only some envelope kinds use, and the difference is load-bearing: a deployed
# Kahala image is pinned by content hash, so a spec-2 Waikiki routinely meets a
# spec-1 Kahala. Stamping every payload with the producer's build number would
# make that peer reject a *page* snapshot whose bytes it understands perfectly —
# a false rejection by the very gate that exists to catch real incompatibility.
# So each kind carries its own floor, and a kind's floor moves only when that
# kind's shape actually changes.

PAGE_ENVELOPE_SPEC = 1
"""Spec floor for a per-page snapshot/changelog — its shape is unchanged since v1."""

WIKI_ENVELOPE_SPEC = 2
"""Spec floor for a wiki bundle — its manifest gained content sections in v2."""

WIKI_CHANGELOG_SPEC = 3
"""Spec floor for a wiki changelog **that carries the v3 sections**.

Stamped per payload, not per kind, and that is the point. A wiki changelog whose
only content is pages has exactly the shape older peers already read, so it
keeps stamping :data:`PAGE_ENVELOPE_SPEC` and a deployed older Kahala can still
answer it. The moment the envelope actually carries elements, templates or
images it stamps this instead — because a peer that cannot read those sections
would otherwise parse the envelope happily and **silently drop the content**,
which is the failure this version exists to prevent. Rejecting loudly is the
whole job of the gate; a stamp that never rises turns it off.

The state vector does the opposite, deliberately: its new sections are a *hint*
about what the sender already holds, so an older peer that ignores them still
behaves correctly — it simply sends back no definitions, exactly as it did
before. It therefore keeps stamping the page floor and never rejects on this
account.
"""


class Compatibility(str, Enum):
    """Outcome of negotiating two peers' protocol versions."""

    COMPATIBLE = "compatible"
    """Identical versions — exchange freely."""

    UPGRADE_ON_IMPORT = "upgrade_on_import"
    """Remote spec is older but still supported — read it in compatibility mode."""

    REJECT = "reject"
    """Versions are incompatible — refuse the exchange."""


@dataclass(frozen=True)
class ProtocolVersions:
    """The version pair carried by a snapshot/changelog envelope."""

    spec_version: int = SPEC_VERSION
    yjs_protocol: int = YJS_SYNC_PROTOCOL_VERSION

    @classmethod
    def current(cls) -> ProtocolVersions:
        """The versions this build produces."""
        return cls(SPEC_VERSION, YJS_SYNC_PROTOCOL_VERSION)


def negotiate(
    remote: ProtocolVersions, local: ProtocolVersions | None = None
) -> tuple[Compatibility, str]:
    """Decide whether ``local`` can consume data produced by ``remote``.

    Returns the :class:`Compatibility` verdict and a human-readable reason. Pure
    and side-effect free so callers can log/telemeter the reason before acting.

    Rules, in order:

    1. Yjs protocol mismatch → REJECT (the CRDT byte format itself differs).
    2. Remote spec newer than local → REJECT (we cannot understand the future;
       the local peer must upgrade).
    3. Remote spec below our floor → REJECT (too old to read).
    4. Remote spec older than local but >= floor → UPGRADE_ON_IMPORT.
    5. Equal spec → COMPATIBLE.
    """
    local = local or ProtocolVersions.current()

    if remote.yjs_protocol != local.yjs_protocol:
        return (
            Compatibility.REJECT,
            f"Yjs protocol mismatch: peer v{remote.yjs_protocol}, local v{local.yjs_protocol}",
        )
    if remote.spec_version > local.spec_version:
        return (
            Compatibility.REJECT,
            f"peer spec v{remote.spec_version} is newer than local "
            f"v{local.spec_version}; local peer must upgrade wiki-interchange",
        )
    if remote.spec_version < MIN_COMPATIBLE_SPEC_VERSION:
        return (
            Compatibility.REJECT,
            f"peer spec v{remote.spec_version} is below the minimum supported "
            f"v{MIN_COMPATIBLE_SPEC_VERSION}",
        )
    if remote.spec_version < local.spec_version:
        return (
            Compatibility.UPGRADE_ON_IMPORT,
            f"peer spec v{remote.spec_version} older than local "
            f"v{local.spec_version}; reading in compatibility mode",
        )
    return Compatibility.COMPATIBLE, "spec and Yjs protocol versions match"


def check_compatible(
    remote: ProtocolVersions, local: ProtocolVersions | None = None
) -> Compatibility:
    """Negotiate versions and raise on the reject path.

    Returns the (non-reject) :class:`Compatibility` verdict, or raises
    :class:`IncompatibleVersionError` when the peers cannot exchange data.
    """
    compat, reason = negotiate(remote, local)
    if compat is Compatibility.REJECT:
        raise IncompatibleVersionError(reason)
    return compat
