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


def test_soft_deleted_pages_excluded_from_search(wiki):
    store.create_page("Ghost", "unique content about aardvarks")
    assert rag.search_pages("aardvarks")            # found while active
    store.soft_delete("ghost")
    assert rag.search_pages("aardvarks") == []      # gone from search
    assert rag.search_chunks("aardvarks") == []
    store.restore("ghost")
    assert rag.search_pages("aardvarks")            # back after restore
