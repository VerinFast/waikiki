"""End-to-end: two agents working in two different wikis must not collide.

Waikiki's central promise is that wikis are *fully* isolated (CLAUDE.md rule 4).
These tests drive the real MCP tool surface and read the results back through the
real HTTP app, so they exercise the whole path an agent actually takes.

They currently FAIL, and the reason is one root cause: **the MCP server's active
wiki is install-global, not per-agent.**

    _ACTIVE: str | None = None                        # module global
    _ACTIVE_FILE = config.DATA_DIR / "mcp_active_wiki"  # one file per install

- `_ACTIVE` is a module global, so any `switch_wiki` retargets every caller in
  that process.
- `_ACTIVE_FILE` persists the choice to a single file shared by *every* MCP
  process on the machine, so a newly started agent silently inherits whichever
  wiki some other agent last selected.

The second is the dangerous one in practice. MCP servers get respawned all the
time (client restart, reconnect, a new session), and on respawn an agent adopts a
foreign wiki without ever calling `switch_wiki` — while its own context still
believes it is in the wiki it chose. Every subsequent write lands in someone
else's wiki, which is exactly "the wrong articles in the wrong wikis".

`switch_wiki`'s docstring says "Switch **your** active wiki", and `current_wiki`
says "which wiki is active **for you**". There is no "you" in the implementation.

Marked xfail(strict=True) so the suite stays usable as a correctness anchor while
the bug is open. strict means these turn into hard failures the moment the bug is
fixed, so nobody can fix it and leave stale xfails behind — delete the markers.
See the tracking issue.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from waikiki import db, mcp_server, store, wikis
from waikiki.api import app


@pytest.fixture
def two_agents(wiki, tmp_path, monkeypatch):
    """Two isolated wikis, and MCP state that can't touch the real install.

    `_ACTIVE_FILE` is bound to the real DATA_DIR at import time, so without this
    redirect these tests would rewrite the live install's active wiki and
    repoint a human's running agents at whatever this test picked last.
    """
    monkeypatch.setattr(mcp_server, "_ACTIVE_FILE", tmp_path / "mcp_active_wiki")
    monkeypatch.setattr(mcp_server, "_ACTIVE", None)
    alpha = wikis.create_wiki("Alpha Project")
    beta = wikis.create_wiki("Beta Project")
    return alpha, beta


def _page_exists_in(slug: str, wiki_slug: str) -> bool:
    """Read a page through the repository, scoped to one wiki."""
    token = db.current_wiki.set(wiki_slug)
    try:
        return store.get_page(slug) is not None
    finally:
        db.current_wiki.reset(token)


def _pages_in(wiki_slug: str) -> list[str]:
    token = db.current_wiki.set(wiki_slug)
    try:
        return sorted(p["slug"] for p in store.list_pages())
    finally:
        db.current_wiki.reset(token)


@pytest.mark.xfail(strict=True, reason="MCP active wiki is install-global, not per-agent")
def test_two_agents_writing_different_wikis_do_not_cross(two_agents):
    """The headline case: A works in Alpha, B works in Beta, concurrently.

    Each agent picks its own wiki and writes its own page. Nothing either agent
    does should be able to move the other one.
    """
    alpha, beta = two_agents

    mcp_server.switch_wiki(alpha)                    # agent A chooses Alpha
    mcp_server.switch_wiki(beta)                     # agent B chooses Beta

    # Agent A never changed its mind, and now writes. This is A's turn, in A's
    # conversation, with A's context saying "I am in Alpha".
    mcp_server.create_page("Alpha Roadmap", "internal to Alpha")

    assert _page_exists_in("alpha-roadmap", alpha), (
        f"agent A's page landed in the wrong wiki: "
        f"alpha={_pages_in(alpha)} beta={_pages_in(beta)}")
    assert not _page_exists_in("alpha-roadmap", beta), \
        "agent A's page leaked into agent B's wiki"


@pytest.mark.xfail(strict=True, reason="MCP active wiki is install-global, not per-agent")
def test_a_fresh_agent_does_not_inherit_another_agents_wiki(two_agents):
    """A newly started MCP process must not adopt another agent's wiki.

    This is the respawn path: clients restart MCP servers routinely, and
    `_load_active()` reads a file the previous agent wrote.
    """
    alpha, _beta = two_agents
    mcp_server.switch_wiki(alpha)                    # agent A chooses Alpha

    # Agent B's MCP process starts up. This is exactly what main() does.
    inherited = mcp_server._load_active()

    assert inherited is None, (
        f"a fresh agent started up already pointed at {inherited!r} — it never "
        "called switch_wiki, so it has no business having an active wiki")


@pytest.mark.xfail(strict=True, reason="MCP active wiki is install-global, not per-agent")
def test_one_agent_cannot_retarget_another_agents_writes(two_agents):
    """B calling switch_wiki must not change where A's next write goes."""
    alpha, beta = two_agents

    mcp_server.switch_wiki(alpha)
    assert mcp_server.current_wiki()["active"] == alpha

    mcp_server.switch_wiki(beta)                      # the other agent, mid-flight

    # From A's point of view nothing happened, so A's view should still be Alpha.
    assert mcp_server.current_wiki()["active"] == alpha, \
        "agent B's switch_wiki silently retargeted agent A"


@pytest.mark.xfail(strict=True, reason="MCP active wiki is install-global, not per-agent")
def test_end_to_end_two_agents_over_http(two_agents):
    """The same failure, verified the way a human would see it in the app.

    Writes go through the MCP tools; reads go through the real HTTP app scoped by
    ?wiki=, i.e. what the human actually browses.

    The turn order is what matters. Each agent declares its wiki once, at the
    start of its own conversation, and then takes turns writing — which is how two
    concurrent sessions actually behave. (Switching immediately before every
    single write happens to work, which is why this can look fine in casual use
    and then corrupt two wikis the moment the turns interleave.)
    """
    alpha, beta = two_agents

    mcp_server.switch_wiki(alpha)        # agent A, start of its conversation
    mcp_server.switch_wiki(beta)         # agent B, start of its conversation
    mcp_server.create_page("Alpha Brief", "alpha-only body text")   # A's turn
    mcp_server.create_page("Beta Brief", "beta-only body text")     # B's turn

    with TestClient(app, client=("127.0.0.1", 1)) as c:
        alpha_page = c.get(f"/wiki/alpha-brief?wiki={alpha}")
        beta_page = c.get(f"/wiki/beta-brief?wiki={beta}")
        # Neither wiki should be able to see the other's page.
        alpha_leak = c.get(f"/wiki/beta-brief?wiki={alpha}")
        beta_leak = c.get(f"/wiki/alpha-brief?wiki={beta}")

    assert alpha_page.status_code == 200, (
        f"Alpha's own page is missing from Alpha "
        f"(alpha={_pages_in(alpha)} beta={_pages_in(beta)})")
    assert beta_page.status_code == 200, "Beta's own page is missing from Beta"
    assert alpha_leak.status_code == 404, "Beta's page is visible inside Alpha"
    assert beta_leak.status_code == 404, "Alpha's page is visible inside Beta"


def test_the_repository_layer_itself_stays_isolated(two_agents):
    """Control: the isolation seam below MCP is sound.

    This one PASSES, which localises the bug. `db.current_wiki` is a contextvar
    and `store` honours it, so the repository is not where this goes wrong — it
    is the MCP layer's process-global notion of "the" active wiki. Any fix should
    keep this passing.
    """
    alpha, beta = two_agents

    token = db.current_wiki.set(alpha)
    try:
        store.create_page("Alpha Only", "a")
    finally:
        db.current_wiki.reset(token)

    token = db.current_wiki.set(beta)
    try:
        store.create_page("Beta Only", "b")
    finally:
        db.current_wiki.reset(token)

    assert _pages_in(alpha) == ["alpha-only"]
    assert _pages_in(beta) == ["beta-only"]
