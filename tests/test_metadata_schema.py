"""Typed page metadata (#28): a template declares the shape, pydantic checks it.

The policy under test is "warn, never block". Every write still lands — a human's
typing is never discarded and an agent's write is never refused — and the
mismatch is *reported* on the Metadata tab and through MCP. The other half is
that a template which declares nothing behaves exactly as it did before, and a
page written before a schema existed is not retroactively broken by one.
"""
from fastapi.testclient import TestClient

from waikiki import db, mcp_server, metaschema, store
from waikiki.api import app

CHARACTER = ("hp: str\n"          # "20 / 100" stays free text
             "blade*: int\n"      # `*` = required, as in custom elements
             "role: player | npc\n"
             "born: date\n"
             "active: bool\n"
             "factions: list[str]\n")


def _client():
    return TestClient(app, client=("127.0.0.1", 1))


def _character_template(markdown="# {{title}}\n\nbody\n"):
    store.template_save("Character", markdown, meta_schema=CHARACTER)
    return store.template_by_name("Character")


# --- The declaration ----------------------------------------------------------

def test_the_declaration_parses_types_required_and_choices():
    fields = {f["name"]: f for f in metaschema.parse_schema(CHARACTER)}
    assert fields["hp"]["type"] == "str" and fields["hp"]["required"] is False
    assert fields["blade"]["type"] == "int" and fields["blade"]["required"] is True
    assert fields["role"]["choices"] == ["player", "npc"]
    assert fields["born"]["type"] == "date"
    assert fields["factions"]["type"] == "list[str]"


def test_the_pydantic_spelling_of_a_choice_is_accepted_too():
    """The ticket sketched `Literal[...]`; both spellings mean one thing."""
    assert metaschema.parse_schema('role: Literal["player", "npc"]')[0]["choices"] \
        == ["player", "npc"]


def test_noise_in_a_declaration_cannot_break_it():
    """A template is edited in a textarea by a human. Comments, blank lines and a
    typo'd type must degrade, never raise — pages made from it still have to
    save."""
    fields = metaschema.parse_schema(
        "# a character\n\nhp: str\nnot a field\nmood: wibble\ncount: int = 0\n")
    assert [f["name"] for f in fields] == ["hp", "mood", "count"]
    assert fields[1]["type"] == "str"        # unknown type -> unconstrained
    assert fields[2]["required"] is False    # `= default` declares optional


def test_values_are_coerced_for_the_reader_not_on_disk():
    out = metaschema.validate(
        {"hp": "20 / 100", "blade": "3", "role": "npc", "born": "2024-02-03",
         "active": "yes", "factions": "vale, spire"}, CHARACTER)
    assert out["ok"] is True and out["errors"] == []
    assert out["values"] == {"hp": "20 / 100", "blade": 3, "role": "npc",
                             "born": "2024-02-03", "active": True,
                             "factions": ["vale", "spire"]}


def test_a_key_matches_the_way_set_properties_matches_it():
    """`Hit Points` and `hitpoints` are the same property everywhere else."""
    out = metaschema.validate({"Hit Points": "12"}, "hitpoints: int\n")
    assert out["ok"] is True and out["values"]["hitpoints"] == 12


# --- Storage: a template carries its schema ------------------------------------

def test_a_template_stores_and_returns_its_schema(wiki):
    _character_template()
    assert store.template_by_name("Character")["meta_schema"] == CHARACTER
    assert store.templates_list()[0]["meta_schema"] is not None
    tid = store.template_by_name("Character")["id"]
    assert store.template_get(tid)["meta_schema"] == CHARACTER


def test_saving_markdown_without_a_schema_keeps_the_one_already_there(wiki):
    """An agent rewriting the body must not silently drop a human's schema."""
    _character_template()
    store.template_save("Character", "# {{title}}\n\nnew body\n")
    tpl = store.template_by_name("Character")
    assert tpl["meta_schema"] == CHARACTER and "new body" in tpl["markdown"]
    store.template_save("Character", "# {{title}}\n", meta_schema="")  # explicit
    assert store.template_by_name("Character")["meta_schema"] == ""


def test_the_column_migration_is_a_no_op_for_an_existing_wiki(wiki):
    """A wiki created before this feature gains an empty column and nothing else."""
    conn = db.get_conn()
    conn.execute("ALTER TABLE templates DROP COLUMN meta_schema")   # pre-#28 shape
    conn.execute("INSERT INTO templates(name, markdown) VALUES ('Old', '# old')")
    conn.commit()
    db._ensure_schema(conn)
    tpl = store.template_by_name("Old")
    assert tpl["markdown"] == "# old"          # the template survived
    assert tpl["meta_schema"] == ""            # and constrains nothing
    assert store.check_metadata({"template": "Old", "anything": "goes"})["ok"]


# --- A page remembers the template it came from --------------------------------

def test_a_page_from_a_typed_template_is_stamped_with_it(wiki):
    tpl = _character_template("---\nhp: 20 / 100\n---\n# {{title}}\n")
    store.create_from_template("Character", "Meru")
    props = store.page_metadata("meru")["properties"]
    assert props["template"] == "Character"
    assert props["hp"] == "20 / 100"           # the template's own frontmatter
    assert tpl["meta_schema"] == CHARACTER


def test_the_human_path_stamps_the_page_the_same_way(wiki):
    """`/new?template=` prefills the editor; both callers get one code path."""
    _character_template()
    with _client() as c:
        body = c.get("/new?template=Character").text
    assert "template: Character" in body


def test_an_untyped_template_produces_exactly_what_it_did_before(wiki):
    """No schema, no stamp, no new property: byte-for-byte the old behaviour."""
    store.template_save("Plain", "# {{title}}\n\nnotes\n")
    store.create_from_template("Plain", "Meru")
    assert store.get_page("meru")["markdown"] == "# Meru\n\nnotes\n"
    meta = store.page_metadata("meru")
    assert meta["properties"] == {}
    assert meta["schema"]["ok"] is True and meta["schema"]["fields"] == []


# --- Reporting, not blocking ---------------------------------------------------

def test_a_page_that_matches_its_schema_reports_ok(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    store.set_properties("meru", {"hp": "20 / 100", "blade": "2",
                                  "role": "npc", "born": "2024-02-03"})
    checked = store.metadata_schema("meru")
    assert checked["template"] == "Character" and checked["ok"] is True
    assert checked["errors"] == [] and checked["values"]["blade"] == 2


def test_a_violating_write_still_lands_and_is_reported(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    store.set_properties("meru", {"blade": "e", "role": "ghost"})

    assert store.get_property("meru", "blade") == "e"      # written, verbatim
    assert "blade: e" in store.get_page("meru")["markdown"]
    checked = store.metadata_schema("meru")
    assert checked["ok"] is False
    assert {e["key"] for e in checked["errors"]} == {"blade", "role"}
    assert "integer" in [e["message"] for e in checked["errors"]
                         if e["key"] == "blade"][0]


def test_a_missing_required_property_is_reported_not_invented(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    checked = store.metadata_schema("meru")
    assert [e["key"] for e in checked["errors"]] == ["blade"]
    assert store.get_property("meru", "blade") is None     # nothing was filled in


def test_page_metadata_carries_the_check(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    store.set_properties("meru", {"blade": "nope"})
    assert store.page_metadata("meru")["schema"]["ok"] is False
    assert store.page_metadata("nope") is None


def test_a_page_bound_to_a_deleted_template_still_reports_ok(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    store.template_delete(store.template_by_name("Character")["id"])
    checked = store.metadata_schema("meru")
    assert checked["ok"] is True and checked["template_found"] is False


# --- Existing pages when a schema arrives later --------------------------------

def test_adding_a_schema_later_does_not_touch_existing_pages(wiki):
    """The migration story: pages made before the schema keep working, unchanged
    and unflagged, because nothing bound them to the template."""
    store.template_save("Character", "# {{title}}\n\nnotes\n")
    store.create_from_template("Character", "Old Timer")
    before = store.get_page("old-timer")["markdown"]

    store.template_save("Character", "# {{title}}\n\nnotes\n",
                        meta_schema=CHARACTER)

    assert store.get_page("old-timer")["markdown"] == before   # not rewritten
    checked = store.metadata_schema("old-timer")
    assert checked["ok"] is True and checked["fields"] == []
    with _client() as c:                                       # and still renders
        assert c.get("/wiki/old-timer").status_code == 200
        assert c.get("/wiki/old-timer/metadata").status_code == 200


def test_an_old_page_can_be_opted_in_by_hand(wiki):
    """Binding is a visible property, so re-binding is an ordinary edit."""
    store.template_save("Character", "# {{title}}\n", meta_schema=CHARACTER)
    store.create_page("Old Timer", "# Old Timer\n")
    assert store.metadata_schema("old-timer")["fields"] == []

    store.set_properties("old-timer", {"template": "Character", "blade": "7"})
    checked = store.metadata_schema("old-timer")
    assert checked["template"] == "Character" and checked["ok"] is True
    assert checked["values"]["blade"] == 7


# --- The human's surface: the Metadata tab ------------------------------------

def test_the_tab_reports_a_mismatch_without_refusing_the_edit(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    with _client() as c:
        r = c.post("/wiki/meru/metadata",
                   data={"key": ["template", "blade", "role"],
                         "value": ["Character", "e", "ghost"]},
                   follow_redirects=False)
        assert r.status_code == 303 and "error=" not in r.headers["location"]
        body = c.get("/wiki/meru/metadata").text

    assert store.get_property("meru", "blade") == "e"   # the typing was kept
    assert "Character" in body and "valid integer" in body
    assert "metarow-bad" in body                        # the row is marked


def test_the_tab_says_so_when_everything_matches(wiki):
    _character_template()
    store.create_from_template("Character", "Meru")
    with _client() as c:
        c.post("/wiki/meru/metadata",
               data={"key": ["template", "blade"], "value": ["Character", "4"]},
               follow_redirects=False)
        body = c.get("/wiki/meru/metadata").text
    assert "Everything matches" in body
    assert "Expects" in body and "player | npc" in body   # what to fill in


def test_an_untyped_page_gets_no_schema_furniture(wiki):
    store.create_page("Meru", "---\nRole: capital\n---\nbody")
    with _client() as c:
        body = c.get("/wiki/meru/metadata").text
    assert "metaschema" not in body and "Checked against" not in body


def test_the_template_editor_round_trips_a_schema(wiki):
    with _client() as c:
        c.post("/templates/save",
               data={"name": "Character", "markdown": "# {{title}}",
                     "meta_schema": CHARACTER, "tid": ""},
               follow_redirects=False)
        tid = store.template_by_name("Character")["id"]
        assert "blade*: int" in c.get(f"/templates/{tid}/edit").text
        c.post("/templates/save",                       # cleared on purpose
               data={"name": "Character", "markdown": "# {{title}}",
                     "meta_schema": "", "tid": str(tid)},
               follow_redirects=False)
    assert store.template_by_name("Character")["meta_schema"] == ""


# --- The agent's surface: MCP --------------------------------------------------

def test_set_metadata_reports_what_the_template_expected(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    _character_template()
    store.create_from_template("Character", "Meru")

    res = mcp_server.set_metadata("meru", {"blade": "e"})
    assert res["properties"]["blade"] == "e"            # written anyway
    assert res["schema"]["ok"] is False
    assert res["schema"]["errors"][0]["key"] == "blade"
    assert res["schema"]["errors"][0]["expected"] == "int (required)"

    ok = mcp_server.set_metadata("meru", {"blade": "3"})
    assert ok["schema"]["ok"] is True


def test_get_metadata_exposes_the_schema_and_coerced_values(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    _character_template()
    store.create_from_template("Character", "Meru")
    store.set_properties("meru", {"blade": "3", "role": "player"})
    got = mcp_server.get_metadata("meru")
    assert got["schema"]["template"] == "Character"
    assert got["schema"]["values"]["blade"] == 3
    assert [f["name"] for f in got["schema"]["fields"]][:2] == ["hp", "blade"]


def test_an_agent_can_declare_a_schema_and_not_clobber_one(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    mcp_server.create_template("Character", "# {{title}}", meta_schema=CHARACTER)
    assert store.template_by_name("Character")["meta_schema"] == CHARACTER
    mcp_server.create_template("Character", "# {{title}}\n\nmore")   # no schema arg
    assert store.template_by_name("Character")["meta_schema"] == CHARACTER
