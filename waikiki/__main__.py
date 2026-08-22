"""Console entry points for the pip-installed package.

The macOS `.app` has its own launcher (`waikiki_app.py`, which owns the native
window); this is the same server without one, for `pip install waikiki` on any
platform. `run.py` at the repo root stays as the from-a-checkout path and both
land on the same `waikiki.api:app`.
"""
from __future__ import annotations


def serve() -> None:
    """`waikiki` — run the web app.

    Binds beyond loopback only when LAN sharing is on *and* a password is set,
    which is the same rule the packaged app follows: turning the port outward is
    a deliberate act, never a default.
    """
    import uvicorn

    from . import auth, config

    bind = config.HOST
    if bind == "127.0.0.1" and auth.share_lan_enabled():
        bind = "0.0.0.0"
    shared = "  (also serving the local network)" if bind == "0.0.0.0" else ""
    print(f"Waikiki on http://{config.HOST}:{config.PORT}{shared}")
    uvicorn.run("waikiki.api:app", host=bind, port=config.PORT, reload=False)


def mcp() -> None:
    """`waikiki-mcp` — run the MCP server over stdio.

    Nothing may write to stdout here: it is the JSON-RPC channel, and a stray
    print corrupts the protocol.
    """
    from . import mcp_server

    mcp_server.main()


if __name__ == "__main__":
    serve()
