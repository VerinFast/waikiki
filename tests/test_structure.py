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


def test_parse_frontmatter_crlf():
    # The web editor's form submit sends Windows CRLF — the fence and values must
    # still parse (regression: CRLF dropped the whole block, losing tags).
    meta, tags, body = structure.parse_frontmatter(
        "---\r\ntags: Character, Spirit\r\nhp: 42\r\n---\r\n# Body\r\ntext")
    assert tags == ["character", "spirit"]
    assert meta == {"hp": "42"}
    assert body.startswith("# Body") and "\r" not in body


def test_crlf_page_indexes_and_stores_lf(wiki):
    store.create_page("Meru", "---\r\ntags: character, spirit\r\n---\r\nA guardian.\r\n")
    assert store.tags_of("meru") == ["character", "spirit"]
    tags = {t["tag"]: t["count"] for t in store.all_tags()}
    assert tags["character"] == 1 and tags["spirit"] == 1
    assert "meru" in {p["slug"] for p in store.pages_with_tag("character")}
    assert "\r" not in store.get_page("meru")["markdown"]   # stored as LF


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


def test_property_interpolation(wiki):
    store.create_page("Meru", "---\nHitPoints: 42\n---\nMeru has {{HitPoints}} HP.")
    store.create_page("Battle", "Meru brings {{Meru.HitPoints}} HP to the fight.")
    assert "42" in store.get_page("meru")["html"]        # {{Key}} local
    assert "42" in store.get_page("battle")["html"]       # {{Slug.Key}} cross-page


def test_set_get_property(wiki):
    store.create_page("Ansel", "# Ansel\nbody text")
    store.set_property("ansel", "Class", "Mage")
    assert store.get_property("ansel", "Class") == "Mage"
    assert "body text" in store.get_page("ansel")["markdown"]   # body preserved
    store.create_page("Ref", "Ansel is a {{Ansel.Class}}.")
    assert "Mage" in store.get_page("ref")["html"]


def test_property_normalized_lookup(wiki):
    store.create_page("Ansel", "---\nHit Points: 10\n---\nbody")
    assert "10" in store.render_html("{{Ansel.HitPoints}}")      # spaceless ref matches


def test_transclusion(wiki):
    store.create_page("Snippet", "Reusable **fact** line.")
    host = store.create_page("Host", "Before.\n\n![[Snippet]]\n\nAfter.")
    assert "Reusable" in host["html"] and "<strong>fact</strong>" in host["html"]
