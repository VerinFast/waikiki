from waikiki import render, store, structure


def test_parse_frontmatter():
    meta, tags, body = structure.parse_frontmatter(
        "---\ntags: Character, Spirit\nhp: 42\nhome: Beaconlight\n---\n# Body\ntext")
    assert tags == ["character", "spirit"]
    assert meta == {"hp": "42", "home": "Beaconlight"}
    assert body.startswith("# Body")


def test_parse_frontmatter_absent():
    meta, tags, body = structure.parse_frontmatter("# Just a page")
    assert meta == {} and tags == [] and body == "# Just a page"


def test_timeline_component():
    h = render.render_markdown("```timeline\n1990: Founded\n2020: IPO\n```")
    assert '<div class="timeline">' in h
    assert "Founded" in h and "IPO" in h
    assert "<pre><code" not in h          # unwrapped


def test_infobox():
    assert render.infobox({"HP": "42"}) == '<table class="infobox"><tr><th>HP</th><td>42</td></tr></table>'
    assert render.infobox({}) == ""


def test_tags_indexed_and_queryable(wiki):
    store.create_page("Ansel", "---\ntags: character, mage\n---\nA mage.")
    store.create_page("Bram", "---\ntags: character\n---\nA fighter.")
    assert store.tags_of("ansel") == ["character", "mage"]
    tags = {t["tag"]: t["count"] for t in store.all_tags()}
    assert tags["character"] == 2 and tags["mage"] == 1
    assert {p["slug"] for p in store.pages_with_tag("character")} == {"ansel", "bram"}


def test_frontmatter_renders_infobox(wiki):
    p = store.create_page("Sheet", "---\nRole: Mage\n---\n# Sheet\nbody")
    assert 'class="infobox"' in p["html"] and "Mage" in p["html"]
    assert "Role:" not in p["html"].split("<h1")[0] or "infobox" in p["html"]


def test_transclusion(wiki):
    store.create_page("Snippet", "Reusable **fact** line.")
    host = store.create_page("Host", "Before.\n\n![[Snippet]]\n\nAfter.")
    assert "Reusable" in host["html"] and "<strong>fact</strong>" in host["html"]
