"""Whole-wiki interchange bundle: gather one wiki, apply it into another (#57).

The per-page snapshot (``test_ydoc_interchange.py``) moves a page. The RFC's D4
asks for the wiki: *"both produce/consume a snapshot (whole wiki) and a changelog
(incremental)"*. These tests hold the real assertion — export a populated wiki,
import it into an empty one, and prove the second **is** the first: pages,
hierarchy, order, starred, custom elements, templates *with* their metadata
schemas, tags, and images.

They also prove the negatives, which matter more than the positives here: the
payload carries no server-only identity/authz field and no embeddings, a shared
image blob is stored once rather than once per page, and a bundle that fails
anywhere is refused before a single local write.
"""
from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from waikiki import db, elements, store, wikis, ydoc
from waikiki.vendor import wiki_interchange as wi


# --- Fixtures / helpers -------------------------------------------------------

def _in(slug: str):
    """Run the following statements against ``slug``'s database."""
    return _Active(slug)


class _Active:
    def __init__(self, slug: str):
        self.slug = slug

    def __enter__(self):
        self._token = db.current_wiki.set(self.slug)
        db.init_db()
        return self.slug

    def __exit__(self, *exc):
        db.current_wiki.reset(self._token)
        return False


PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 40


def _populate() -> dict:
    """Build a wiki that exercises every part of the envelope. Returns a summary."""
    image_id = store.save_image("logo.png", "image/png", PNG)

    home = store.create_page("Home", f"---\ntags: intro, wiki\n---\n# Home\n\n"
                                     f"![logo](/image/{image_id})\n")
    guide = store.create_page("Guide", f"How to.\n\n![logo](/image/{image_id})\n")
    child = store.create_page("Deep Dive", "A sub-page.")
    store.set_parent(child["slug"], guide["slug"])
    store.toggle_star(home["slug"])
    store.set_page_order([guide["slug"], home["slug"]])

    # A renamed page: the slug is stable, so it no longer matches its title.
    # Common in a real wiki, and the case where deriving a slug from the title
    # on import would land the page somewhere nothing in the bundle references.
    renamed = store.create_page("Roadmap", "Where we are going.")
    store.update_page(renamed["slug"], "2027 Roadmap", "Where we are going.")
    store.set_parent(renamed["slug"], home["slug"])

    elements.save_element(
        "callout", "Callout",
        [{"name": "tone", "required": True, "label": "Tone"}],
        "<div class='c'><slot></slot></div>", ".c{color:red}", "export default 1;",
    )
    store.template_save("Meeting", "---\ntemplate: Meeting\n---\n# {{title}}",
                        meta_schema="attendees[*]: str\ndate: date")
    return {"image_id": image_id}


def _fingerprint() -> dict:
    """Everything the round-trip is supposed to preserve, as plain data."""
    pages = {}
    for row in store._export_page_rows():
        pages[row["slug"]] = {
            "title": row["title"],
            "markdown": row["markdown"],
            "parent_slug": row["parent_slug"],
            "sort_order": row["sort_order"],
            "starred": bool(row["starred"]),
            "tags": store.tags_of(row["slug"]),
        }
    return {
        "pages": pages,
        "elements": {e["slug"]: e for e in elements.list_elements()},
        "templates": {t["name"]: {"markdown": t["markdown"],
                                  "meta_schema": t["meta_schema"]}
                      for t in store.templates_list()},
    }


def _image_bytes(slug_markdown: str) -> bytes:
    """The bytes of the single image a body references, as stored locally."""
    ids = {int(m) for m in ydoc._IMG_REF.findall(slug_markdown)}
    assert len(ids) == 1, f"expected one image reference, found {sorted(ids)}"
    return bytes(store.get_image(ids.pop())["data"])


@pytest.fixture
def two_wikis(wiki):
    """A populated source wiki and an empty destination, in one temp data dir."""
    src = wikis.create_wiki("Source Wiki")
    dst = wikis.create_wiki("Destination Wiki")
    with _in(src):
        _populate()
    return src, dst


# --- The round trip -----------------------------------------------------------

def test_whole_wiki_round_trips_into_an_empty_wiki(two_wikis):
    src, dst = two_wikis
    with _in(src):
        before = _fingerprint()
        raw = store.export_wiki_bundle()
    with _in(dst):
        summary = store.import_wiki_bundle(raw)
        after = _fingerprint()

    assert summary["pages"] == 4
    # A renamed page keeps the slug the bundle's hierarchy references, not one
    # re-derived from its title.
    assert after["pages"]["roadmap"]["title"] == "2027 Roadmap"
    assert after["pages"]["roadmap"]["parent_slug"] == "home"
    # Pages, titles, bodies, hierarchy, order and starred all survive.
    assert set(after["pages"]) == set(before["pages"])
    for slug, was in before["pages"].items():
        assert after["pages"][slug] == was, f"page {slug!r} did not round-trip"
    # Custom elements and templates — including the metadata schema — survive.
    assert after["elements"]["callout"] == before["elements"]["callout"]
    assert after["templates"]["Meeting"] == before["templates"]["Meeting"]
    assert after["templates"]["Meeting"]["meta_schema"].startswith("attendees[*]")


def test_hierarchy_survives_although_page_ids_do_not(two_wikis):
    """The hierarchy is expressed by slug, so it holds even though every page
    lands under a different integer id in the destination."""
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()
        src_ids = {p["slug"]: p["id"] for p in store._export_page_rows()}
    with _in(dst):
        # Make the destination hand out different ids than the source did.
        for i in range(5):
            store.create_page(f"Filler {i}", "x")
        store.import_wiki_bundle(raw)
        rows = {p["slug"]: p for p in store._export_page_rows()}

    assert rows["deep-dive"]["parent_slug"] == "guide"
    assert rows["deep-dive"]["id"] != src_ids["deep-dive"], \
        "ids must differ, or this test proves nothing"


def test_tags_ride_in_the_page_and_land_in_the_local_index(two_wikis):
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()
    with _in(dst):
        store.import_wiki_bundle(raw)
        assert store.tags_of("home") == ["intro", "wiki"]
        assert [p["slug"] for p in store.pages_with_tag("intro")] == ["home"]


def test_a_shared_image_lands_once_and_both_pages_point_at_it(two_wikis):
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()
    with _in(dst):
        summary = store.import_wiki_bundle(raw)
        home, guide = store.get_page("home"), store.get_page("guide")
        # Both bodies were rewritten to the new local id — the same one.
        assert _image_bytes(home["markdown"]) == PNG
        assert _image_bytes(guide["markdown"]) == PNG
        home_ids = set(ydoc._IMG_REF.findall(home["markdown"]))
        assert home_ids == set(ydoc._IMG_REF.findall(guide["markdown"]))
    assert summary["images"] == 1, "the shared blob was stored more than once"


def test_the_bundle_carries_one_copy_of_a_shared_blob(two_wikis):
    src, _dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        blobs = [n for n in archive.namelist() if n.startswith("images/")]
        assert len(blobs) == 1
        assert archive.read(blobs[0]) == PNG
        # …and no page snapshot still carries the bytes inline.
        for name in (n for n in archive.namelist() if n.startswith("pages/")):
            assert PNG not in archive.read(name)


def test_the_canonical_ydoc_lineage_crosses_with_the_page(two_wikis):
    """The imported page keeps the sender's CRDT lineage, so a later changelog
    against it is a delta rather than a full resend."""
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()
        src_state = ydoc.state_vector(store.get_page("home"))
    with _in(dst):
        store.import_wiki_bundle(raw)
        assert ydoc.state_vector(store.get_page("home")) == src_state


def test_import_merges_by_slug_and_never_deletes_local_pages(two_wikis):
    src, dst = two_wikis
    with _in(dst):
        store.create_page("Home", "local text")        # same slug, different body
        store.create_page("Local Only", "keep me")
    with _in(src):
        raw = store.export_wiki_bundle()
    with _in(dst):
        store.import_wiki_bundle(raw)
        assert "Home" in store.get_page("home")["markdown"]      # updated in place
        assert store.get_page("local-only") is not None          # left alone
        # The overwrite is versioned like any other edit, not a silent clobber.
        assert len(store.page_versions("home")) >= 2


# --- Prove the negatives ------------------------------------------------------

def _keys(obj) -> set:
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= _keys(v)
    elif isinstance(obj, list):
        for i in obj:
            out |= _keys(i)
    return out


def test_the_payload_carries_no_server_only_field(two_wikis):
    """Content only: the bundle must not name a tenant, a wiki, or a permission.

    Waikiki is the single-owner local side — it has no tenancy to leak — but the
    payload it produces is what Kahala imports *up*, so a server-only field here
    is exactly the field an import could try to escalate with. The server
    re-attaches identity from its own context; the format never carries it.
    """
    src, _dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()

    forbidden = wi.SERVER_ONLY_FIELD_NAMES
    # Structural keys are checked against the whole forbidden set. The raw-byte
    # scan below can't tell a key from a value, so it uses only the names no
    # human writes in prose — the source wiki deliberately tags a page "wiki",
    # and content that merely *says* a forbidden word must still export.
    unambiguous = [n for n in forbidden if "_" in n or n in ("membership", "casbin")]

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert not {k for k in _keys(manifest) if k.casefold() in forbidden}
        for name in (n for n in archive.namelist() if n.startswith("pages/")):
            envelope = json.loads(archive.read(name))
            assert not {k for k in _keys(envelope) if k.casefold() in forbidden}
            # Yjs encodes Map keys as literal UTF-8, so a smuggled key would show
            # up in the raw CRDT bytes even if it never appeared as JSON.
            state = base64.b64decode(envelope["ydoc_state"])
            for field in unambiguous:
                assert field.encode("utf-8") not in state


def test_the_payload_carries_no_embeddings_or_chunks(two_wikis):
    """Derived data is regenerated on import, never shipped — a vector from a
    different embedding model is worse than no vector at all."""
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert not (_keys(manifest) & wi.DERIVED_FIELD_NAMES)
        assert not {n for n in archive.namelist()
                    if "embedding" in n or "chunk" in n or "vec" in n}
    for banned in (b"embedding", b"chunks", b"vec_chunks"):
        assert banned not in raw

    # …and the destination still ends up searchable, because it re-embeds.
    with _in(dst):
        store.import_wiki_bundle(raw)
        rows = db.get_conn().execute(
            "SELECT COUNT(*) n FROM chunks").fetchone()
        assert dict(rows)["n"] > 0, "import did not rebuild the local index"


def test_an_incompatible_bundle_is_refused_not_merged(two_wikis):
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()
    tampered = _retag(raw, spec_version=wi.SPEC_VERSION + 1)   # a peer from the future

    with _in(dst):
        with pytest.raises(wi.IncompatibleVersionError):
            store.import_wiki_bundle(tampered)
        assert store.list_pages(include_children=True) == []


def test_a_bundle_that_fails_midway_writes_nothing(two_wikis):
    """All-or-nothing against a bad payload: page 3 of 3 being corrupt must not
    leave pages 1 and 2 (or the elements, or the images) behind."""
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    members["pages/home.snapshot"] = b"not a snapshot envelope"
    broken = _rebuild(members)

    with _in(dst):
        with pytest.raises(wi.InterchangeError):
            store.import_wiki_bundle(broken)
        assert store.list_pages(include_children=True) == []
        assert store.get_page("guide") is None
        assert store.template_by_name("Meeting") is None
        assert elements.get_element("callout") is None


def test_a_substituted_image_blob_is_refused(two_wikis):
    """The sidecar is content-addressed; bytes that do not match the digest they
    were stored under must not land in the local image store."""
    src, dst = two_wikis
    with _in(src):
        raw = store.export_wiki_bundle()

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    blob_name = next(n for n in members if n.startswith("images/"))
    members[blob_name] = b"malicious payload"

    with _in(dst):
        with pytest.raises(wi.MalformedEnvelopeError):
            store.import_wiki_bundle(_rebuild(members))
        assert store.get_image(1) is None


def test_export_never_ships_a_trashed_page(two_wikis):
    src, _dst = two_wikis
    with _in(src):
        store.soft_delete("home")
        raw = store.export_wiki_bundle()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert [p["slug"] for p in manifest["pages"]] == ["deep-dive", "guide", "roadmap"]


# --- Streaming ----------------------------------------------------------------

def test_export_streams_into_a_file(two_wikis, tmp_path):
    """The file form is the real one: a wiki here is 215 pages / ~57MB, so the
    export must not require the whole thing in memory."""
    src, dst = two_wikis
    path = tmp_path / "wiki.bundle"
    with _in(src):
        with open(path, "wb") as fh:
            assert store.export_wiki_bundle(fh) is None
    with _in(dst):
        with open(path, "rb") as fh:
            summary = store.import_wiki_bundle(fh)
    assert summary["pages"] == 4


# --- Helpers that rewrite an archive (test-only) ------------------------------

def _rebuild(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buf.getvalue()


def _retag(raw: bytes, **fields) -> bytes:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest.update(fields)
    members["manifest.json"] = json.dumps(manifest).encode("utf-8")
    return _rebuild(members)
