"""End-to-end: two agents working in two different wikis must not collide.

Waikiki's central promise is that wikis are *fully* isolated (CLAUDE.md rule 4).
These tests drive the real MCP tool surface and read the results back through the
real HTTP app, so they exercise the whole path an agent actually takes.

They were written as failing tests for issue #11, where the active wiki was a
module global persisted to a single file in DATA_DIR:

    _ACTIVE: str | None = None                          # module global
    _ACTIVE_FILE = config.DATA_DIR / "mcp_active_wiki"   # one file per install

Any `switch_wiki` retargeted every caller in the process, and the file was shared
by *every* MCP process on the machine — so a respawned agent silently inherited
whichever wiki another agent had last chosen and kept writing there, while its
own context still believed it was elsewhere. Two agents produced
``alpha=[] beta=['alpha-brief', 'beta-brief']``: both agents' pages in one wiki,
the other empty.

The fix scopes the active wiki to the agent's session and stops persisting it, so
there is nothing to inherit. These tests pin that contract:

- one agent's `switch_wiki` cannot move another agent
- a fresh session starts with **no** active wiki and is refused until it chooses
- nothing is written to disk, so a restart can't resurrect a foreign wiki
- writes land in the session's own wiki, verified through the HTTP app

`_SESSION_OVERRIDE` is how a test acts as a distinct agent without a live MCP
connection; in production the key comes from FastMCP's session/client id.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from waikiki import config, db, mcp_server, store, wikis
from waikiki.api import app


@pytest.fixture
def two_agents(wiki, monkeypatch):
    """Two isolated wikis and clean per-session MCP state."""
    monkeypatch.setattr(mcp_server, "_ACTIVE", None)
    monkeypatch.setattr(mcp_server, "_ACTIVE_BY_SESSION", {})
    alpha = wikis.create_wiki("Alpha Project")
    beta = wikis.create_wiki("Beta Project")
    return alpha, beta


class _Agent:
    """One agent's MCP session.

    Inside the `with`, tool calls are attributed to this session — the same way a
    real connection's session id scopes them in production.
    """

    def __init__(self, name: str):
        self.name = name
        self._token = None

    def __enter__(self):
        self._token = mcp_server._SESSION_OVERRIDE.set(self.name)
        return self

    def __exit__(self, *exc):
        mcp_server._SESSION_OVERRIDE.reset(self._token)
        return False


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


def test_two_agents_writing_different_wikis_do_not_cross(two_agents):
    """The headline case: A works in Alpha, B works in Beta, concurrently.

    Turn order is what used to break this. Each agent declares its wiki once at
    the start of its own conversation, then they take turns — switching
    immediately before every single write happened to work, which is why the bug
    could hide in casual use and then corrupt two wikis.
    """
    alpha, beta = two_agents

    with _Agent("agent-a"):
        mcp_server.switch_wiki(alpha)
    with _Agent("agent-b"):
        mcp_server.switch_wiki(beta)

    # A's turn. A never changed its mind; its context says "I am in Alpha".
    with _Agent("agent-a"):
        mcp_server.create_page("Alpha Roadmap", "internal to Alpha")
    # B's turn.
    with _Agent("agent-b"):
        mcp_server.create_page("Beta Roadmap", "internal to Beta")

    assert _pages_in(alpha) == ["alpha-roadmap"], (
        f"Alpha holds the wrong pages: "
        f"alpha={_pages_in(alpha)} beta={_pages_in(beta)}")
    assert _pages_in(beta) == ["beta-roadmap"], (
        f"Beta holds the wrong pages: "
        f"alpha={_pages_in(alpha)} beta={_pages_in(beta)}")


def test_a_fresh_agent_does_not_inherit_another_agents_wiki(two_agents):
    """A newly connected agent must have no active wiki and be refused.

    This is the respawn path: clients restart MCP servers routinely. Silent
    inheritance was the dangerous part, because the agent had no way to notice.
    """
    alpha, _beta = two_agents
    with _Agent("agent-a"):
        mcp_server.switch_wiki(alpha)

    with _Agent("agent-b"):                      # connects for the first time
        assert mcp_server.current_wiki()["active"] is None, \
            "a fresh agent started up already pointed at another agent's wiki"
        with pytest.raises(RuntimeError, match="No active wiki"):
            mcp_server.create_page("Should Not Exist", "x")

    assert _pages_in(alpha) == [], "the refused write still reached Alpha"


def test_one_agent_cannot_retarget_another(two_agents):
    """Each agent's view of "my wiki" is independent of the other's."""
    alpha, beta = two_agents

    with _Agent("agent-a"):
        mcp_server.switch_wiki(alpha)
    with _Agent("agent-b"):
        mcp_server.switch_wiki(beta)             # must not move agent A

    with _Agent("agent-a"):
        assert mcp_server.current_wiki()["active"] == alpha, \
            "agent B's switch_wiki silently retargeted agent A"
    with _Agent("agent-b"):
        assert mcp_server.current_wiki()["active"] == beta


def test_the_active_wiki_is_never_written_to_disk(two_agents):
    """No on-disk state means a restart has nothing to inherit.

    The original bug was a single file in DATA_DIR shared by every MCP process on
    the machine. Persisting the active wiki anywhere reintroduces that, so this
    guards the whole class rather than one filename.
    """
    alpha, beta = two_agents
    before = {p.name for p in config.DATA_DIR.iterdir()}

    with _Agent("agent-a"):
        mcp_server.switch_wiki(alpha)
    with _Agent("agent-b"):
        mcp_server.switch_wiki(beta)

    new_files = {p.name for p in config.DATA_DIR.iterdir()} - before
    assert not new_files, (
        f"switch_wiki persisted state to disk: {sorted(new_files)} — a shared "
        "file is how agents inherited each other's wikis")


def test_end_to_end_two_agents_over_http(two_agents):
    """Verified the way a human sees it: MCP writes, browsed over HTTP.

    Reads go through the real app scoped by ?wiki=, i.e. what the human browses.
    """
    alpha, beta = two_agents

    with _Agent("agent-a"):
        mcp_server.switch_wiki(alpha)
    with _Agent("agent-b"):
        mcp_server.switch_wiki(beta)
    with _Agent("agent-a"):
        mcp_server.create_page("Alpha Brief", "alpha-only body text")
    with _Agent("agent-b"):
        mcp_server.create_page("Beta Brief", "beta-only body text")

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


def test_live_edit_headers_carry_each_agents_own_wiki(two_agents):
    """The HTTP injection path must be scoped per agent too.

    Live edits POST to the web app with X-Waikiki-Wiki; if that header came from
    shared state, two agents editing concurrently would inject into each other's
    wiki even with the pointer fixed.
    """
    alpha, beta = two_agents

    with _Agent("agent-a"):
        mcp_server.switch_wiki(alpha)
    with _Agent("agent-b"):
        mcp_server.switch_wiki(beta)

    with _Agent("agent-a"):
        assert mcp_server._headers()["X-Waikiki-Wiki"] == alpha
    with _Agent("agent-b"):
        assert mcp_server._headers()["X-Waikiki-Wiki"] == beta


def test_the_repository_layer_itself_stays_isolated(two_agents):
    """Control: the isolation seam below MCP is sound.

    `db.current_wiki` is a contextvar and `store` honours it, which is what
    localised the original bug to the MCP layer. Any future change to how the
    active wiki is tracked must keep this passing.
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
