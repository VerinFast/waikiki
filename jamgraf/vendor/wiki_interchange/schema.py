# Last scanned for code smell: 2026-08-05 by Claude.
"""The canonical Y.Doc shared-type layout for a wiki page/doc.

A single wiki page is one ``pycrdt.Doc`` with a fixed set of root shared types.
This module owns three things:

* the **schema** — the root names, their CRDT types, and the shape of the
  structured nodes stored inside them (see the table below);
* the **constructor** — :func:`build_page_doc`, the only sanctioned way to mint a
  conformant doc from plain Python content; and
* the **guards** — :func:`assert_content_only` (no server-side identity/authz
  fields) and :func:`assert_no_derived_data` (no RAG chunks/embeddings), which
  the snapshot/changelog encoders call so a non-conformant payload fails closed.

Root shared types (spec v1)::

    content   Text    the page's markdown body (matches Waikiki's "content" key)
    tree      Array   ordered page-tree nodes: {node_id, slug, title, parent_id}
    comments  Array   {comment_id, author, body, anchor, created_at, resolved}
    tags      Array   list[str]
    elements  Array   custom elements: {element_id, kind, name, attrs{...}}
    meta      Map     {schema_version, title, slug, created_at, updated_at}

Note what is deliberately **absent**: no ``tenant_id``, ``wiki_id``, membership
or permissions (those are server-owned — see :data:`SERVER_ONLY_FIELD_NAMES`),
and no ``chunks``/``embeddings`` (derived data, regenerated on import — see
:data:`DERIVED_FIELD_NAMES`). ``author`` on a comment is a content-level display
name, not an authz principal.
"""

from __future__ import annotations

from typing import Any

from pycrdt import Array, Doc, Map, Text

from .errors import DerivedDataError, ServerFieldLeakError
from .version import SPEC_VERSION

# --- Root shared-type names (the schema's public vocabulary) ---------------
ROOT_CONTENT = "content"
ROOT_TREE = "tree"
ROOT_COMMENTS = "comments"
ROOT_TAGS = "tags"
ROOT_ELEMENTS = "elements"
ROOT_META = "meta"

#: Root name -> CRDT type. Used to materialise a decoded doc for validation.
ROOT_TYPES: dict[str, type] = {
    ROOT_CONTENT: Text,
    ROOT_TREE: Array,
    ROOT_COMMENTS: Array,
    ROOT_TAGS: Array,
    ROOT_ELEMENTS: Array,
    ROOT_META: Map,
}

# --- Forbidden field names -------------------------------------------------
#: Server-owned identity/authz keys. The interchange format carries content
#: only; these belong to the server (re-attached and re-validated against Casbin
#: on import-up, which is K3's job, not this library's). Compared case-folded.
SERVER_ONLY_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "tenant_id",
        "tenant",
        "wiki_id",
        "wiki",
        "membership",
        "members",
        "member",
        "permissions",
        "permission",
        "acl",
        "owner_org",
        "org_relation",
        "casbin",
    }
)

#: Derived-data keys. Vectors/chunks are regenerated on import so model or
#: dimension drift never travels with content. Compared case-folded.
DERIVED_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "embedding",
        "embeddings",
        "chunk",
        "chunks",
        "vector",
        "vectors",
        "search_tsv",
    }
)


def build_page_doc(
    *,
    content: str = "",
    title: str | None = None,
    slug: str | None = None,
    tree: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    elements: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> Doc:
    """Construct a conformant wiki-page :class:`~pycrdt.Doc` from plain content.

    Accepts *content only* — there is intentionally no ``tenant_id`` / ``wiki_id``
    parameter, so a local import cannot carry tenancy that could escalate. The
    result is validated with :func:`assert_content_only` and
    :func:`assert_no_derived_data` before it is returned.
    """
    doc = Doc()
    doc[ROOT_CONTENT] = Text(content or "")
    doc[ROOT_TREE] = Array(list(tree or []))
    doc[ROOT_COMMENTS] = Array(list(comments or []))
    doc[ROOT_TAGS] = Array(list(tags or []))
    doc[ROOT_ELEMENTS] = Array(list(elements or []))

    meta_map: dict[str, Any] = {"schema_version": SPEC_VERSION}
    if title is not None:
        meta_map["title"] = title
    if slug is not None:
        meta_map["slug"] = slug
    if meta:
        meta_map.update(meta)
    doc[ROOT_META] = Map(meta_map)

    assert_content_only(doc)
    assert_no_derived_data(doc)
    return doc


def _root_to_py(doc: Doc, key: str) -> Any:
    """Materialise a single root shared type as plain Python for walking."""
    declared = ROOT_TYPES.get(key)
    for candidate in (declared, Map, Array, Text):
        if candidate is None:
            continue
        try:
            value = doc.get(key, type=candidate)
        except Exception:  # noqa: BLE001 - wrong type guess; try the next
            continue
        if candidate is Text:
            return str(value)
        if candidate is Array:
            return list(value)
        if candidate is Map:
            return dict(value)
    return None


def _keys_in(obj: Any) -> set[str]:
    """Recursively collect every mapping key inside a plain-Python structure."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                found.add(key)
            found |= _keys_in(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found |= _keys_in(item)
    return found


def collect_field_names(doc: Doc) -> set[str]:
    """Every key name that appears anywhere in ``doc`` (root names + nested keys).

    Content *values* (markdown text, tags, titles, comment bodies) are not
    included — only structural keys, which is where an identity/authz field would
    have to live. That keeps a page whose prose merely mentions ``tenant_id``
    from tripping the guards.
    """
    names: set[str] = set()
    for root in doc.keys():
        names.add(root)
        names |= _keys_in(_root_to_py(doc, root))
    return names


def _match(names: set[str], forbidden: frozenset[str]) -> set[str]:
    return {n for n in names if n.casefold() in forbidden}


def assert_content_only(doc: Doc) -> None:
    """Raise :class:`ServerFieldLeakError` if any server-only key is present."""
    leaked = _match(collect_field_names(doc), SERVER_ONLY_FIELD_NAMES)
    if leaked:
        raise ServerFieldLeakError(
            "server-only identity/authz fields must not appear in the "
            f"interchange format: {sorted(leaked)}"
        )


def assert_no_derived_data(doc: Doc) -> None:
    """Raise :class:`DerivedDataError` if derived (RAG/embedding) keys are present."""
    derived = _match(collect_field_names(doc), DERIVED_FIELD_NAMES)
    if derived:
        raise DerivedDataError(
            "derived data (RAG chunks / embeddings) is excluded from the "
            f"interchange format and regenerated on import: {sorted(derived)}"
        )
