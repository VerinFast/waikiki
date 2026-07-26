from waikiki import imagegen, shellenv, store


def test_article_image_dir_created(wiki):
    d = imagegen.article_image_dir("main", "reef")
    assert d.exists() and d.name == "reef" and d.parent.name == "main"


def test_next_filename_increments(wiki):
    d = imagegen.article_image_dir("main", "reef")
    n1 = imagegen._next_filename(d, "A Blue Fish")
    assert n1.startswith("a-blue-fish") and n1.endswith("-1.png")
    (d / n1).write_bytes(b"x")
    assert imagegen._next_filename(d, "A Blue Fish").endswith("-2.png")


def test_write_image_prompt_falls_back_without_claude(monkeypatch):
    monkeypatch.setattr(shellenv, "which", lambda name: None)
    assert imagegen.write_image_prompt("T", "body", "a red car") == "a red car"


def test_generate_empty_description(wiki):
    store.create_page("Reef", "# Reef")
    assert imagegen.generate("reef", "   ")["ok"] is False


def test_generate_missing_cli(wiki, monkeypatch):
    monkeypatch.setattr(shellenv, "which", lambda name: None)
    store.create_page("Reef", "# Reef")
    out = imagegen.generate("reef", "a wave", image_cli="agy")
    assert out["ok"] is False and "agy" in out["error"]
