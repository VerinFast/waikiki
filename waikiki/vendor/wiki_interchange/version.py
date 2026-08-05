# Last scanned for code smell: 2026-08-05 by Claude.
"""Spec + Yjs sync protocol versions and the compatibility gate.

Two independent version numbers travel in every snapshot/changelog envelope:

* ``SPEC_VERSION`` — the good-place wiki-interchange *spec* version. Bump it when
  the envelope shape or the Y.Doc shared-type layout changes in a way peers must
  agree on.
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

SPEC_VERSION = 1
"""Current wiki-interchange spec version emitted by this build."""

MIN_COMPATIBLE_SPEC_VERSION = 1
"""Oldest spec version this build can still read (the upgrade-on-import floor)."""

YJS_SYNC_PROTOCOL_VERSION = 1
"""Yjs update binary protocol version. Must match exactly between peers."""


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
            f"Yjs protocol mismatch: peer v{remote.yjs_protocol}, " f"local v{local.yjs_protocol}",
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
