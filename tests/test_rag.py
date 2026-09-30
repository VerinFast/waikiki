from waikiki import config, rag, store


def test_chunk_text_splits_long_input():
    text = "word " * 800  # ~4000 chars
    chunks = rag.chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= config.CHUNK_CHARS + 50 for c in chunks)


def test_chunk_text_empty():
    assert rag.chunk_text("") == []


def test_bm25_page_search(wiki):
    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    store.create_page("Trails", "hiking routes through alpine forests")
    hits = rag.search_pages("coffee pressure")
    assert hits and hits[0]["slug"] == "espresso"


def test_hybrid_chunk_search_returns_source(wiki):
    store.create_page("Kayaking", "the eskimo roll rights a capsized kayak")
    results = rag.search_chunks("kayak roll", k=3)
    assert results
    assert results[0]["slug"] == "kayaking"
    assert "score" in results[0]


def test_search_no_match_is_empty(wiki):
    store.create_page("Only", "single page about tomatoes")
    assert rag.search_pages("xyzzyqwerty") == []


def test_hybrid_user_search_all_pages(wiki):
    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    store.create_page("Trails", "hiking routes through alpine forests")
    res = rag.search("coffee pressure")               # default index='all'
    assert res and res[0]["slug"] == "espresso"
    assert "<mark>" in res[0]["snip"]                 # keyword terms highlighted
    assert rag.search("coffee", mode="keyword")       # BM25 only
    assert rag.search("coffee", mode="semantic")      # embeddings (FakeEmbedder)


def test_indices_scoping_and_exclude_children(wiki):
    store.create_page("Bestiary", "an index of monsters")
    store.create_page("Gearwyrm", "a clockwork serpent haunting the foundry depths")
    store.set_parent("gearwyrm", "bestiary")          # moves child to sub-index

    keys = [ix["key"] for ix in rag.list_indices()]
    assert keys[0] == "all" and "parent:bestiary" in keys

    # default 'all' search INCLUDES the child (main bar behavior)...
    assert "gearwyrm" in [r["slug"] for r in rag.search("clockwork serpent")]
    # ...exclude_children restricts to top-level (keyword and semantic)...
    assert "gearwyrm" not in [r["slug"] for r in rag.search("clockwork serpent", exclude_children=True)]
    assert "gearwyrm" not in [r["slug"] for r in rag.search("clockwork", mode="semantic", exclude_children=True)]
    # ...and its parent's partition returns it.
    assert "gearwyrm" in [r["slug"] for r in rag.search("clockwork serpent", index="parent:bestiary")]


def test_soft_deleted_pages_excluded_from_search(wiki):
    store.create_page("Ghost", "unique content about aardvarks")
    assert rag.search_pages("aardvarks")            # found while active
    store.soft_delete("ghost")
    assert rag.search_pages("aardvarks") == []      # gone from search
    assert rag.search_chunks("aardvarks") == []
    store.restore("ghost")
    assert rag.search_pages("aardvarks")            # back after restore


# --- Index health -------------------------------------------------------------

def test_stale_index_reason_is_quiet_on_a_healthy_wiki(wiki):
    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    assert rag.stale_index_reason() is None
    assert rag.reindex_if_stale() == 0


def test_stale_index_reason_ignores_an_empty_wiki(wiki):
    """Nothing to index is not a broken index."""
    assert rag.stale_index_reason() is None


def test_stale_index_reason_flags_missing_chunks(wiki):
    from waikiki import db

    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    db.get_conn().execute("DELETE FROM chunks")
    assert "no chunks" in rag.stale_index_reason()
    assert rag.reindex_if_stale() == 1
    assert rag.stale_index_reason() is None


def test_stale_index_reason_flags_a_dimension_mismatch(wiki, monkeypatch):
    """Vectors from a machine that embedded at another width.

    Left alone this is worse than it looks: `db.ensure_vec_table` rebuilds a vec0
    table whose dimension changed, and it runs on the *search* path — so the
    first query silently discards the imported vectors.

    `db.VEC_AVAILABLE` is forced on so the judgement is testable where
    sqlite-vec isn't installed; only the judgement is under test here, not the
    vec0 storage.
    """
    from waikiki import db

    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    monkeypatch.setattr(db, "VEC_AVAILABLE", True)
    db.set_setting("vec_dim", "1024")               # the fake embedder is dim 8
    reason = rag.stale_index_reason()
    assert reason and "1024-dim" in reason and "8-dim" in reason


def test_stale_index_reason_accepts_bm25_only_when_nothing_can_embed(wiki, monkeypatch):
    """No usable embedder: re-chunking would drop vectors to replace them with
    nothing, so BM25 alone is the honest state, not a stale index."""
    from waikiki import db, embeddings

    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    monkeypatch.setattr(db, "VEC_AVAILABLE", True)
    db.set_setting("vec_dim", "1024")

    def no_embedder():
        raise RuntimeError("no model on this machine")

    monkeypatch.setattr(embeddings, "get_embedder", no_embedder)
    assert rag.stale_index_reason() is None
