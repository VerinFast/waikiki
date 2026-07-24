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

        # --- HTML views render ---
        assert c.get("/").status_code == 200
        assert c.get("/settings").status_code == 200
        assert c.get("/wiki/reef").status_code == 200
