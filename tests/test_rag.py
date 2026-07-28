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


def test_hybrid_user_search_main_index(wiki):
    store.create_page("Espresso", "a concentrated coffee brewed under pressure")
    store.create_page("Trails", "hiking routes through alpine forests")
    res = rag.search("coffee pressure", index="main", mode="hybrid")
    assert res and res[0]["slug"] == "espresso"
    assert "<mark>" in res[0]["snip"]                 # keyword terms highlighted
    assert rag.search("coffee", mode="keyword")       # BM25 only
    assert rag.search("coffee", mode="semantic")      # embeddings (FakeEmbedder)


def test_list_indices_and_partition_scoping(wiki):
    store.create_page("Bestiary", "an index of monsters")
    store.create_page("Gearwyrm", "a clockwork serpent haunting the foundry depths")
    store.set_parent("gearwyrm", "bestiary")          # moves child to sub-index

    keys = [ix["key"] for ix in rag.list_indices()]
    assert keys[0] == "main" and "parent:bestiary" in keys

    # child is excluded from the main index (both keyword and semantic)...
    assert "gearwyrm" not in [r["slug"] for r in rag.search("clockwork serpent")]
    assert "gearwyrm" not in [r["slug"] for r in rag.search("clockwork", mode="semantic")]
    # ...but found when searching its parent's partition
    sub = rag.search("clockwork serpent", index="parent:bestiary")
    assert "gearwyrm" in [r["slug"] for r in sub]


def test_soft_deleted_pages_excluded_from_search(wiki):
    store.create_page("Ghost", "unique content about aardvarks")
    assert rag.search_pages("aardvarks")            # found while active
    store.soft_delete("ghost")
    assert rag.search_pages("aardvarks") == []      # gone from search
    assert rag.search_chunks("aardvarks") == []
    store.restore("ghost")
    assert rag.search_pages("aardvarks")            # back after restore
