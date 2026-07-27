"""Regression tests for the CRDT edit path.

pycrdt/yrs Text indexes in UTF-8 bytes, but edit planners produce Python
code-point offsets. Editing at/after a multi-byte character (emoji, accents) used
to land at the wrong byte position — dropping/overwriting characters at the edit
border, or panicking on a character boundary.
"""
import anyio
from pycrdt import Doc, Text

from waikiki import collab, edits, store


def test_byte_offset_conversion():
    assert collab._byte_offset("a\U0001F33Ab", 2) == 5     # code-point 2 → byte 5
    assert collab._byte_offset("abc", 2) == 2               # ascii unchanged
    assert collab._byte_offset("café", 4) == 5         # é is 2 bytes


def _crdt_splice(text, planner):
    """Mirror collab.apply_edit's CRDT operations on a raw Text."""
    doc = Doc()
    t = doc.get("content", type=Text)
    with doc.transaction():
        t += text
    start, end, insert = planner(text)
    bs, be = collab._byte_offset(text, start), collab._byte_offset(text, end)
    with doc.transaction():
        if be > bs:
            del t[bs:be]
        if insert:
            t.insert(bs, insert)
    return str(t)


def test_crdt_splice_matches_pure_across_multibyte():
    cases = [
        ("a\U0001F33Ab", {"op": "edit", "old": "b", "new": "Z"}),
        ("x\U0001F33Ay\U0001F33Az", {"op": "edit", "old": "y", "new": "QQ"}),
        ("# \U0001F33A Meru\nHit Points: 10\nbody",
         {"op": "edit", "old": "Hit Points: 10", "new": "Hit Points: 42"}),
        ("café ☕ shop", {"op": "edit", "old": "shop", "new": "STORE"}),
        ("plain ascii here", {"op": "edit", "old": "ascii", "new": "XYZ"}),
        ("\U0001F33A\U0001F33A tail", {"op": "insert", "text": "X", "after": "\U0001F33A\U0001F33A "}),
    ]
    for text, spec in cases:
        planner = edits.make_planner(spec)
        assert _crdt_splice(text, planner) == edits.apply_to_string(text, planner), text


def test_apply_edit_real_path_with_emoji(wiki):
    async def go():
        async with collab.server:
            await collab.replace_text("main", "t", "# \U0001F33A Meru\n\nHit Points: 10\n\nTail.")
            res = await collab.apply_edit(
                "main", "t",
                lambda s: edits.plan_edit(s, "Hit Points: 10", "Hit Points: 42"))
            return res, await collab.live_markdown("main", "t")

    store.create_page("T", "seed")
    res, md = anyio.run(go)
    assert res["ok"], res
    assert "Hit Points: 42" in md and "\U0001F33A Meru" in md and "Tail." in md
