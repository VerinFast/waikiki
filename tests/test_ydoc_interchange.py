"""Canonical Y.Doc persistence + wiki-interchange round-trip (W2, #3391).

Covers the four behaviours W2 adds on top of K1's repository chokepoint:

* the Y.Doc is the *canonical persisted state* (``pages.markdown`` is a
  projection of it), round-tripping across reconstruction;
* snapshot **export/import** fidelity (content + title + tags + image sidecar),
  across a wiki boundary;
* incremental **changelog** sync between two wikis; and
* the version **gate reject** path when consuming an incompatible envelope.
"""
from __future__ import annotations

import json

import pytest

from waikiki import db, store, ydoc
from waikiki.vendor import wiki_interchange as wi
from waikiki.vendor.wiki_interchange import (
    ROOT_TAGS,
    IncompatibleVersionError,
    SPEC_VERSION,
    YJS_SYNC_PROTOCOL_VERSION,
)
from pycrdt import Array, Doc


# --- Y.Doc as the canonical persisted state ----------------------------------

def test_canonical_state_persisted_on_create(wiki):
    p = store.create_page("Canon", "hello **world**")
    # A canonical Y.Doc row exists and its content is the page body.
    assert ydoc.has_state(p["id"])
    doc = ydoc.canonical_doc(p)
    assert ydoc.content_of(doc) == "hello **world**"


def test_markdown_is_projection_of_ydoc(wiki):
    store.create_page("Proj", "v1")
    store.update_page("proj", "Proj", "v2 body", author="ai")
    page = store.get_page("proj")
    # The persisted Y.Doc — reconstructed from bytes — matches the projection.
    doc = ydoc.canonical_doc(page)
    assert ydoc.content_of(doc) == "v2 body" == page["markdown"]


def test_tags_and_title_sync_into_canonical_doc(wiki):
    md = "---\ntags: alpha, beta\n---\nbody"
    p = store.create_page("Tagged", md)
    doc = ydoc.canonical_doc(p)
    assert [str(t) for t in list(doc.get(ROOT_TAGS, type=Array))] == ["alpha", "beta"]


def test_canonical_doc_advances_incrementally(wiki):
    """Editing advances the *same* lineage, so a changelog is a delta, not a
    full resend — the property that makes canonical persistence useful for sync."""
    store.create_page("Grow", "one")
    # A peer that holds the page at its initial state.
    peer = Doc()
    peer.apply_update(wi.Snapshot.deserialize(store.export_snapshot("grow")).ydoc_state)
    peer_sv = wi.state_vector(peer)
    # A later edit advances the same lineage; the delta since the peer's state
    # vector converges the peer onto the new content.
    store.update_page("grow", "Grow", "one two three", author="human")
    delta = store.export_changelog("grow", peer_sv)
    wi.apply_changelog(peer, delta)
    assert ydoc.content_of(peer) == "one two three"


def test_lazy_seed_for_legacy_pages(wiki):
    """A page whose canonical row is missing (pre-W2 data) is seeded on demand."""
    p = store.create_page("Legacy", "legacy body")
    ydoc.delete(p["id"])
    assert not ydoc.has_state(p["id"])
    doc = ydoc.canonical_doc(store.get_page("legacy"))
    assert ydoc.content_of(doc) == "legacy body"
    assert ydoc.has_state(p["id"])        # seeded + persisted


# --- Snapshot export / import fidelity (across a wiki boundary) ---------------

def _switch(slug: str):
    db.current_wiki.set(slug)


def test_snapshot_export_import_roundtrip(wiki):
    _switch("main")
    store.create_page("Round Trip", "---\ntags: x, y\n---\nthe body text")
    raw = store.export_snapshot("round-trip")

    _switch("beaconlight")
    assert store.list_pages() == []       # starts empty
    page = store.import_snapshot(raw)
    assert page["title"] == "Round Trip"
    assert "the body text" in page["markdown"]
    doc = ydoc.canonical_doc(page)
    assert [str(t) for t in list(doc.get(ROOT_TAGS, type=Array))] == ["x", "y"]


def test_snapshot_image_sidecar_roundtrip(wiki):
    _switch("main")
    blob = b"\x89PNG\r\n\x1a\nFAKEIMAGE"
    iid = store.save_image("pic.png", "image/png", blob)
    store.create_page("Has Image", f"see ![pic](/image/{iid}/pic.png) here")
    raw = store.export_snapshot("has-image")

    _switch("beaconlight")
    page = store.import_snapshot(raw)
    # The blob was re-homed content-addressed and the body points at the new id.
    import re
    m = re.search(r"/image/(\d+)", page["markdown"])
    assert m, page["markdown"]
    new_img = store.get_image(int(m.group(1)))
    assert new_img is not None
    assert bytes(new_img["data"]) == blob


def test_snapshot_is_content_only(wiki):
    """The exported envelope carries no server-only identity fields."""
    _switch("main")
    store.create_page("Clean", "just content")
    raw = store.export_snapshot("clean")
    doc = wi.decode_snapshot(raw)         # re-runs the content-only guard
    names = {n.casefold() for n in wi.collect_field_names(doc)}
    assert not (names & wi.SERVER_ONLY_FIELD_NAMES)
    assert not (names & wi.DERIVED_FIELD_NAMES)


# --- Changelog sync between two wikis ----------------------------------------

def test_changelog_sync_between_wikis(wiki):
    # Seed main, hydrate beaconlight from a snapshot so both share a lineage.
    _switch("main")
    store.create_page("Sync Page", "line one")
    snap = store.export_snapshot("sync-page")
    _switch("beaconlight")
    store.import_snapshot(snap)
    peer_sv = store.page_state_vector("sync-page")

    # Edit only in main, then ship the delta down to beaconlight.
    _switch("main")
    store.update_page("sync-page", "Sync Page", "line one\nline two", author="human")
    changelog = store.export_changelog("sync-page", peer_sv)

    _switch("beaconlight")
    merged = store.import_changelog("sync-page", changelog)
    assert merged is not None
    assert "line two" in merged["markdown"]
    assert ydoc.content_of(ydoc.canonical_doc(merged)) == merged["markdown"]


def test_import_changelog_missing_page_returns_none(wiki):
    _switch("main")
    store.create_page("Only Here", "body")
    cl = store.export_changelog("only-here", store.page_state_vector("only-here"))
    _switch("beaconlight")
    assert store.import_changelog("only-here", cl) is None


# --- Version-gate reject path (consuming an incompatible envelope) ------------

def _tamper(raw: bytes, **fields) -> bytes:
    env = json.loads(raw)
    env.update(fields)
    return json.dumps(env).encode("utf-8")


def test_version_gate_rejects_newer_spec_snapshot(wiki):
    store.create_page("Gate", "body")
    raw = store.export_snapshot("gate")
    bad = _tamper(raw, spec_version=SPEC_VERSION + 1)
    with pytest.raises(IncompatibleVersionError):
        store.import_snapshot(bad)


def test_version_gate_rejects_yjs_mismatch_snapshot(wiki):
    store.create_page("Gate2", "body")
    raw = store.export_snapshot("gate2")
    bad = _tamper(raw, yjs_protocol=YJS_SYNC_PROTOCOL_VERSION + 1)
    with pytest.raises(IncompatibleVersionError):
        store.import_snapshot(bad)


def test_version_gate_rejects_incompatible_changelog(wiki):
    _switch("main")
    store.create_page("Gate3", "body")
    snap = store.export_snapshot("gate3")
    _switch("beaconlight")
    store.import_snapshot(snap)
    sv = store.page_state_vector("gate3")
    _switch("main")
    store.update_page("gate3", "Gate3", "body more", author="human")
    raw = store.export_changelog("gate3", sv)
    bad = _tamper(raw, spec_version=SPEC_VERSION + 1)
    _switch("beaconlight")
    with pytest.raises(IncompatibleVersionError):
        store.import_changelog("gate3", bad)


def test_version_gate_rejects_below_floor(wiki):
    store.create_page("Gate4", "body")
    raw = store.export_snapshot("gate4")
    bad = _tamper(raw, spec_version=0)
    with pytest.raises(IncompatibleVersionError):
        store.import_snapshot(bad)


# --- Review follow-ups (Copilot on #7) ---------------------------------------

def test_image_sidecar_blob_must_match_its_hash(wiki):
    """Blobs arrive from a peer, so the envelope's claims aren't evidence.
    Accepting bytes under a hash that isn't theirs breaks content-addressing."""
    import hashlib

    import pytest

    from waikiki import ydoc
    from waikiki.vendor import wiki_interchange as wi

    good = b"\x89PNG\r\n\x1a\n-real-bytes"
    honest = wi.ImageRef(image_id="1", media_type="image/png",
                         sha256=hashlib.sha256(good).hexdigest(),
                         filename="a.png", byte_size=len(good), data=good)
    assert ydoc._rehome_images([honest])          # honest blob is accepted

    tampered = wi.ImageRef(image_id="2", media_type="image/png",
                           sha256=hashlib.sha256(b"something else").hexdigest(),
                           filename="b.png", byte_size=len(good), data=good)
    with pytest.raises(wi.MalformedEnvelopeError):
        ydoc._rehome_images([tampered])

    wrong_size = wi.ImageRef(image_id="3", media_type="image/png",
                             sha256=hashlib.sha256(good).hexdigest(),
                             filename="c.png", byte_size=len(good) + 99, data=good)
    with pytest.raises(wi.MalformedEnvelopeError):
        ydoc._rehome_images([wrong_size])


def test_image_ref_pattern_does_not_match_partial_ids():
    from waikiki import ydoc

    assert ydoc._IMG_REF.findall("![a](/image/12/pic.png)") == ["12"]
    assert ydoc._IMG_REF.findall("![a](/image/12)") == ["12"]
    # "/image/12abc" is not image 12 — matching it would rewrite the wrong id
    assert ydoc._IMG_REF.findall("/image/12abc") == []
    assert ydoc._remap_image_ids("/image/12abc", {12: 99}) == "/image/12abc"
    assert ydoc._remap_image_ids("/image/12/p.png", {12: 99}) == "/image/99/p.png"
