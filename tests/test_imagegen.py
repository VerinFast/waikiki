from waikiki import clirun, db, imagegen, shellenv, store


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


def test_style_refs_crud(wiki):
    assert imagegen.list_style_refs("main") == []
    imagegen.save_style_ref("main", "ref.png", b"\x89PNG\r\n")
    imagegen.save_style_ref("main", "../evil.png", b"x")     # path-traversal sanitized
    refs = imagegen.list_style_refs("main")
    assert "ref.png" in refs and "evil.png" in refs
    assert imagegen.delete_style_ref("main", "ref.png") is True
    assert "ref.png" not in imagegen.list_style_refs("main")


def test_reference_images_reach_render_add_dir(wiki, monkeypatch):
    store.create_page("Reef", "# Reef\nbody")
    imagegen.save_style_ref("main", "style1.png", b"\x89PNG\r\n")
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)

    calls = {}

    def fake_run(label, argv, timeout):
        calls[label] = argv
        if label.endswith(":image"):
            (imagegen.article_image_dir("main", "reef") / "out-1.png").write_bytes(b"\x89PNG")

        class P:
            stdout = ""
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    assert imagegen.generate("reef", "a fish", image_cli="agy")["ok"] is True
    argv = calls["agy:image"]
    assert argv.count("--add-dir") == 2                       # article + style refs
    assert str(imagegen.style_reference_dir("main")) in argv


def test_house_style_reaches_the_render_call(wiki, monkeypatch):
    """The per-wiki house style must be woven into the image CLI instruction."""
    store.create_page("Reef", "# Reef\nbody")
    db.set_setting("image_style_prompt", "clockwork punk pixel art")
    monkeypatch.setattr(shellenv, "which", lambda name: "/usr/bin/" + name)

    calls = {}

    def fake_run(label, argv, timeout):
        calls[label] = argv
        if label.endswith(":image"):        # simulate the CLI writing a PNG
            folder = imagegen.article_image_dir("main", "reef")
            (folder / "styled-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        class P:
            stdout = ""
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(clirun, "run", fake_run)
    out = imagegen.generate("reef", "a gear golem", image_cli="agy")
    assert out["ok"] is True
    render_argv = " ".join(map(str, calls["agy:image"]))
    assert "clockwork punk pixel art" in render_argv
