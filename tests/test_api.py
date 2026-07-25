"""End-to-end REST + collaboration-injection tests via FastAPI's TestClient.

Kept in one test so the app lifespan (which starts the CRDT server) runs once.
"""
from fastapi.testclient import TestClient

from waikiki.api import app


def test_rest_and_collab_flow(wiki):
    with TestClient(app) as c:
        # --- create + read ---
        r = c.post("/api/pages", json={"title": "Reef", "markdown": "coral reef life"})
        assert r.status_code == 200
        slug = r.json()["slug"]
        assert slug == "reef"

        assert c.get("/api/pages").json()[0]["slug"] == "reef"
        assert c.get("/api/pages/reef").json()["title"] == "Reef"
        assert c.get("/api/pages/nope").status_code == 404

        # --- update ---
        r = c.put("/api/pages/reef", json={"title": "Reef", "markdown": "reef and fish"})
        assert r.status_code == 200 and "fish" in r.json()["markdown"]

        # --- search ---
        hits = c.get("/api/search", params={"q": "reef fish"}).json()["results"]
        assert hits and hits[0]["slug"] == "reef"

        # --- images ---
        r = c.post("/api/images", files={"file": ("d.png", b"\x89PNG", "image/png")})
        body = r.json()
        assert body["url"].startswith("/image/")
        assert c.get(body["url"]).status_code == 200

        # --- live collaboration injection (what MCP calls) ---
        r = c.post("/api/collab/reef/append", json={"text": "\n\n## Depths"})
        assert r.status_code == 200
        live = c.get("/api/collab/reef/live").json()["markdown"]
        assert "## Depths" in live

        r = c.post("/api/collab/reef/replace", json={"markdown": "wiped"})
        assert r.status_code == 200
        assert c.get("/api/collab/reef/live").json()["markdown"] == "wiped"

        # --- targeted, merge-safe edit (what the AI should use) ---
        c.post("/api/collab/reef/replace", json={"markdown": "alpha beta gamma"})
        assert c.post("/api/collab/reef/edit",
                      json={"old": "beta", "new": "BETA"}).json()["ok"] is True
        assert c.get("/api/collab/reef/live").json()["markdown"] == "alpha BETA gamma"
        assert c.post("/api/collab/reef/edit",
                      json={"old": "nope", "new": "x"}).json()["ok"] is False

        # --- structured text ops via /op ---
        c.post("/api/collab/reef/replace", json={"markdown": "## A\nalpha\n\n## B\nbeta"})
        assert c.post("/api/collab/reef/op",
                      json={"op": "replace_section", "heading": "A",
                            "markdown": "## A\nNEW"}).json()["ok"] is True
        live = c.get("/api/collab/reef/live").json()["markdown"]
        assert "## A\nNEW" in live and "## B\nbeta" in live and "alpha" not in live
        assert c.post("/api/collab/reef/op",
                      json={"op": "prepend", "text": "TOP"}).json()["ok"] is True
        assert c.get("/api/collab/reef/live").json()["markdown"].startswith("TOP")

        # --- change feed + broken-links pages render ---
        assert c.get("/changes").status_code == 200
        assert c.get("/broken-links").status_code == 200

        # --- HTML views render ---
        assert c.get("/").status_code == 200
        assert c.get("/settings").status_code == 200
        assert c.get("/wiki/reef").status_code == 200
        assert c.get("/wikis").status_code == 200

        # --- Connect page carries a valid, copy-paste Claude config ---
        connect_html = c.get("/connect").text
        assert "mcpServers" in connect_html and "WAIKIKI_DATA" in connect_html
        # --- /help redirects into the Help wiki ---
        assert c.get("/help", follow_redirects=False).status_code in (303, 307)
