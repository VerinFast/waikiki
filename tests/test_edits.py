import pytest

from waikiki import edits


def test_plan_edit_unique():
    assert edits.plan_edit("a foo b", "foo", "BAR") == (2, 5, "BAR")


def test_plan_edit_not_found_and_ambiguous():
    with pytest.raises(ValueError):
        edits.plan_edit("abc", "zzz", "x")
    with pytest.raises(ValueError):
        edits.plan_edit("x x", "x", "y")


def test_plan_remove():
    assert edits.apply_to_string("hello world", lambda s: edits.plan_remove(s, " world")) == "hello"


def test_plan_prepend_adds_newline():
    assert edits.apply_to_string("body", lambda s: edits.plan_prepend(s, "top")) == "top\nbody"


def test_plan_insert_after_before_position():
    s = "one two three"
    assert edits.apply_to_string(s, lambda x: edits.plan_insert(x, "!", after="one")) == "one! two three"
    assert edits.apply_to_string(s, lambda x: edits.plan_insert(x, "!", before="two")) == "one !two three"
    assert edits.apply_to_string(s, lambda x: edits.plan_insert(x, "X", position=0)) == "Xone two three"


def test_section_span_and_replace():
    md = "# Title\n\nintro\n\n## A\nalpha\n\n## B\nbeta\n"
    span = edits.section_span(md, "A")
    assert md[span[0]:span[1]] == "## A\nalpha\n\n"
    out = edits.apply_to_string(md, lambda s: edits.plan_replace_section(s, "A", "## A\nNEW"))
    assert "## A\nNEW" in out and "## B\nbeta" in out and "alpha" not in out


def test_section_span_ambiguous_returns_none():
    assert edits.section_span("## A\nx\n## A\ny", "A") is None


def test_make_planner_unknown():
    assert edits.make_planner({"op": "bogus"}) is None
