"""Canonical Y.Doc persistence + wiki-interchange produce/consume.

Waikiki's page content is a **CRDT**, not a string. Historically the ``pycrdt``
Y.Doc was a transient in-memory editing buffer (``collab.py``) that got flushed
to the ``pages.markdown`` column, and SQLite was the only durable truth. This
module **promotes the Y.Doc to the canonical persisted state**: the full encoded
Y.Doc for every page is stored in ``page_ydoc`` and is the source of truth, while
``pages.markdown``/``html`` become a *projection* kept alongside it for FTS,
rendering and RAG.

Because the persisted state is a real CRDT (it accumulates history across saves
and process restarts, keyed by state vector), it is exactly what the Kahala <->
Waikiki round-trip needs. This module wires that round-trip through the vendored,
version-pinned ``wiki_interchange`` format:

* **produce** — :func:`export_snapshot` (full Y.Doc + content-addressed image
  sidecar) and :func:`export_changelog` (updates since a peer's state vector),
  for *import-up* to Kahala.
* **consume** — :func:`decode_snapshot` / :func:`apply_changelog`, honouring the
  spec/Yjs version gate (an incompatible envelope raises, it never merges), for
  *export-down* from Kahala.

Content-only boundary: Waikiki is the single-owner local side. It never invents
or requires ``tenant_id`` / ``wiki_id`` / permissions in the interchange payload
(those are re-attached server-side by Kahala), and local RAG embeddings are
regenerated on import, never shipped. The vendored guards enforce this.

This module sits *below* the repository chokepoint (``store``), like ``rag`` and
``render``: it calls ``db``/``store`` directly and ``store`` orchestrates it, so
routes never touch CRDT state directly.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Optional

from pycrdt import Array, Doc, Map, Text

from . import db, structure
from .vendor import wiki_interchange as wi
from .vendor.wiki_interchange.schema import ROOT_CONTENT, ROOT_META, ROOT_TAGS

# Local image references in page markdown: ![alt](/image/<id>/<name>), or the
# bare ![alt](/image/<id>) form. The trailing guard stops "/image/12abc" from
# matching as image 12 — a false positive would mis-rewrite the id on import.
_IMG_REF = re.compile(r"/image/(\d+)(?![\w-])")


# --- Persistence primitives ---------------------------------------------------

def _load_state(page_id: int) -> Optional[bytes]:
    row = db.get_conn().execute(
        "SELECT ydoc_state FROM page_ydoc WHERE page_id=?", (page_id,)
    ).fetchone()
    if not row:
        return None
    state = row["ydoc_state"]
    # apsw hands BLOBs back as bytes already; be tolerant of memoryview too.
    return bytes(state) if state is not None else None


def _store_state(page_id: int, state: bytes) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO page_ydoc(page_id, ydoc_state, spec_version, updated_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(page_id) DO UPDATE SET ydoc_state=excluded.ydoc_state, "
        "spec_version=excluded.spec_version, updated_at=excluded.updated_at",
        (page_id, bytes(state), wi.SPEC_VERSION),
    )
    conn.commit()


def has_state(page_id: int) -> bool:
    """Whether a page already has a persisted canonical Y.Doc."""
    return _load_state(page_id) is not None


def persist(page_id: int, doc: Doc) -> None:
    """Persist ``doc`` as the page's canonical state (full encoded update)."""
    _store_state(page_id, doc.get_update())


# --- Building / loading the canonical doc ------------------------------------

def _new_doc(content: str, title: str, slug: str, tags: list[str]) -> Doc:
    return wi.build_page_doc(
        content=content or "", title=title, slug=slug, tags=list(tags or [])
    )


def canonical_doc(page: dict) -> Doc:
    """The authoritative Y.Doc for ``page``.

    Reconstructs the persisted CRDT state, or — for a page written before
    Y.Doc-canonical persistence — lazily seeds a conformant doc from the current
    ``markdown`` projection and persists it, so every page has canonical state.
    """
    state = _load_state(page["id"])
    if state is not None:
        doc = Doc()
        doc.apply_update(state)
        return doc
    _meta, tags, _body = structure.parse_frontmatter(page["markdown"])
    doc = _new_doc(page["markdown"], page["title"], page["slug"], list(tags))
    _store_state(page["id"], doc.get_update())
    return doc


def content_of(doc: Doc) -> str:
    """The page markdown carried by a doc's ``content`` Text root.

    This is the whole stored markdown, frontmatter included — it is synced from
    ``pages.markdown`` verbatim, not the body with frontmatter stripped."""
    return str(doc.get(ROOT_CONTENT, type=Text))


# --- Keeping the canonical doc in step with a projection write ----------------

def title_of(doc: Doc) -> Optional[str]:
    """The title carried by a doc's ``meta`` Map (None if it carries none)."""
    val = dict(doc.get(ROOT_META, type=Map)).get("title")
    return str(val) if val is not None else None


def tags_of(doc: Doc) -> list[str]:
    """Tags from a doc's ``tags`` root (the interchange's canonical home)."""
    return [str(t) for t in list(doc.get(ROOT_TAGS, type=Array))]


def reconcile_tags(doc: Doc, before_root: list[str],
                   before_frontmatter: list[str]) -> str:
    """Make a merged doc's two tag representations agree, last write winning.

    Tags live twice over: the ``tags`` root (what the interchange schema syncs)
    and the ``tags:`` line of the frontmatter inside ``content`` (what Waikiki
    renders and indexes). A merge can advance either one, and whichever the
    incoming update actually moved is the newer intent — so that side wins and is
    written into the other. If both moved, or neither did, the ``tags`` root wins,
    since that is the field the format defines for the purpose.

    Returns the reconciled markdown (frontmatter rewritten when needed).
    """
    from . import structure

    content = content_of(doc)
    meta, fm_tags, body = structure.parse_frontmatter(content)
    root_tags = tags_of(doc)

    root_moved = root_tags != list(before_root or [])
    fm_moved = fm_tags != list(before_frontmatter or [])

    if fm_moved and not root_moved:
        _apply_tags(doc, fm_tags)                    # frontmatter came in last
        return content
    if root_tags == fm_tags:
        return content                               # already agree

    # The tags root is newer (or it's a tie) — project it into the frontmatter so
    # the render/index projection sees what the canonical doc actually says.
    lines = [f"{k}: {v}" for k, v in meta.items()]
    if root_tags:
        lines.insert(0, "tags: " + ", ".join(root_tags))
    rebuilt = (("---\n" + "\n".join(lines) + "\n---\n") if lines else "") \
        + body.lstrip("\n")
    _apply_content(doc, rebuilt)
    return rebuilt


def _apply_content(doc: Doc, markdown: str) -> None:
    txt = doc.get(ROOT_CONTENT, type=Text)
    if str(txt) == (markdown or ""):
        return
    with doc.transaction():
        if len(txt):
            del txt[0:len(txt)]
        if markdown:
            txt += markdown


def _apply_tags(doc: Doc, tags: list[str]) -> None:
    arr = doc.get(ROOT_TAGS, type=Array)
    tags = list(tags or [])
    if list(arr) == tags:
        return
    with doc.transaction():
        while len(arr):
            del arr[len(arr) - 1]
        for t in tags:
            arr.append(t)


def _apply_meta(doc: Doc, title: str, slug: str) -> None:
    meta = doc.get(ROOT_META, type=Map)
    with doc.transaction():
        if meta.get("title") != title:
            meta["title"] = title
        if meta.get("slug") != slug:
            meta["slug"] = slug


def sync(page_id: int, slug: str, title: str, markdown: str,
         tags: list[str]) -> None:
    """Advance a page's canonical Y.Doc to match a projection write.

    Called by the repository on every content write so the persisted CRDT stays
    the source of truth. The doc's lineage (state vector) advances in place, so
    later changelog sync stays genuinely incremental.
    """
    state = _load_state(page_id)
    if state is None:
        _store_state(page_id, _new_doc(markdown, title, slug, tags).get_update())
        return
    doc = Doc()
    doc.apply_update(state)
    _apply_content(doc, markdown)
    _apply_tags(doc, list(tags or []))
    _apply_meta(doc, title, slug)
    _store_state(page_id, doc.get_update())


def delete(page_id: int) -> None:
    """Drop a page's canonical state (hard delete; cascade also covers it)."""
    conn = db.get_conn()
    conn.execute("DELETE FROM page_ydoc WHERE page_id=?", (page_id,))
    conn.commit()


# --- Image sidecar (content-addressed, tenant-neutral) ------------------------

def image_refs(markdown: str) -> list["wi.ImageRef"]:
    """Content-addressed :class:`ImageRef`s for the local images a page embeds.

    Carries only content-level fields (sha256, media type, filename, inline
    bytes) — no bucket/path/tenant — matching the interchange security boundary.
    """
    from . import store

    refs: list[wi.ImageRef] = []
    for iid in sorted({int(m) for m in _IMG_REF.findall(markdown or "")}):
        img = store.get_image(iid)
        if not img:
            continue
        data = bytes(img["data"])
        refs.append(
            wi.ImageRef(
                image_id=str(iid),
                media_type=img["mimetype"],
                sha256=hashlib.sha256(data).hexdigest(),
                filename=img.get("filename"),
                byte_size=len(data),
                data=data,
            )
        )
    return refs


def _rehome_images(refs: list["wi.ImageRef"]) -> dict[int, int]:
    """Re-home imported image blobs into the local store; old id -> new local id.

    Blobs arrive from a peer, so the envelope's own claims about them are not
    evidence. Each blob is re-hashed and its length checked before it is stored:
    the sidecar is content-addressed, and accepting bytes under a hash that isn't
    theirs would break de-duplication and let corrupted or substituted data land
    under a trusted digest."""
    from . import store

    remap: dict[int, int] = {}
    for ref in refs:
        if ref.data is None:
            continue  # blob-by-reference fetch is a server concern, not local
        actual = hashlib.sha256(ref.data).hexdigest()
        if ref.sha256 and not hmac.compare_digest(actual, str(ref.sha256).lower()):
            raise wi.MalformedEnvelopeError(
                f"image {ref.image_id}: sidecar blob does not match its sha256 "
                f"(claimed {str(ref.sha256)[:12]}…, actual {actual[:12]}…)")
        if ref.byte_size is not None and int(ref.byte_size) != len(ref.data):
            raise wi.MalformedEnvelopeError(
                f"image {ref.image_id}: byte_size {ref.byte_size} does not match "
                f"the {len(ref.data)} bytes supplied")
        new_id = store.save_image(
            ref.filename or f"{ref.sha256[:12]}", ref.media_type, ref.data
        )
        try:
            remap[int(ref.image_id)] = new_id
        except (TypeError, ValueError):
            pass
    return remap


def _remap_image_ids(text: str, remap: dict[int, int]) -> str:
    if not remap:
        return text

    def repl(m: re.Match) -> str:
        old = int(m.group(1))
        return f"/image/{remap.get(old, old)}"

    return _IMG_REF.sub(repl, text)


# --- Produce (export up to Kahala) -------------------------------------------

def export_snapshot(page: dict) -> bytes:
    """Serialize a page as a wiki-interchange snapshot (Y.Doc + image sidecar)."""
    doc = canonical_doc(page)
    return wi.encode_snapshot(doc, image_refs(page["markdown"])).serialize()


def export_changelog(page: dict, since_state_vector: bytes) -> bytes:
    """Serialize the updates a peer at ``since_state_vector`` is missing."""
    doc = canonical_doc(page)
    return wi.produce_changelog(doc, since_state_vector).serialize()


def state_vector(page: dict) -> bytes:
    """A page's Yjs state vector — what a peer would send to request a changelog."""
    return wi.state_vector(canonical_doc(page))


# --- Consume (import down from Kahala) ---------------------------------------

class ImportedPage:
    """Decoded, content-only import payload ready to project into the store."""

    __slots__ = ("doc", "content", "title", "slug", "tags")

    def __init__(self, doc: Doc, content: str, title: Optional[str],
                 slug: Optional[str], tags: list[str]):
        self.doc = doc
        self.content = content
        self.title = title
        self.slug = slug
        self.tags = tags


def decode_snapshot(raw: bytes) -> ImportedPage:
    """Decode a snapshot envelope, honouring the version gate.

    Rehomes any image blobs from the sidecar into the local store and remaps the
    ``/image/<id>`` references in the body accordingly, so the imported doc and
    its markdown projection agree. Raises the vendored ``IncompatibleVersionError``
    on a spec/Yjs mismatch — an incompatible envelope is refused, never merged.
    """
    snap = wi.Snapshot.deserialize(raw)          # runs the version gate
    doc = wi.decode_snapshot(snap)               # + content-only re-validation
    remap = _rehome_images(snap.images)
    content = _remap_image_ids(content_of(doc), remap)
    if remap:
        _apply_content(doc, content)             # keep the canonical doc in step
    meta = dict(doc.get(ROOT_META, type=Map))
    tags = [str(t) for t in list(doc.get(ROOT_TAGS, type=Array))]
    return ImportedPage(doc, content, meta.get("title"), meta.get("slug"), tags)


def apply_changelog(page: dict, raw: bytes) -> Doc:
    """Merge an incoming changelog into a page's canonical doc (version-gated)."""
    doc = canonical_doc(page)
    wi.apply_changelog(doc, raw)                 # runs the version gate, merges
    return doc
