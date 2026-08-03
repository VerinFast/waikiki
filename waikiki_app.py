#!/usr/bin/env python3
"""Waikiki desktop app.

Runs the FastAPI server in a background thread and shows it in a native macOS
WKWebView window via pywebview. This is the entrypoint PyInstaller bundles into
Waikiki.app.

    python waikiki_app.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

import uvicorn

from waikiki import config
from waikiki.api import app as fastapi_app


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def _pick_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        if not _port_open(host, port):
            return port
    return start


def main() -> None:
    # MCP mode: run the stdio MCP server instead of the window. Lets Claude
    # Desktop launch the packaged .app directly as an MCP server (no source
    # checkout needed) — see the in-app Help page for the copy-paste config.
    if os.environ.get("WAIKIKI_MCP") or "--mcp" in sys.argv:
        from waikiki import mcp_server
        mcp_server.main()
        return

    # Bind beyond loopback only when the owner has turned on LAN sharing (and set
    # a password) — see waikiki/auth.py. Read at startup, so toggling it needs a
    # restart; that keeps the port closed by default.
    #
    # `host` stays the LOOPBACK address the window and health checks use: the auth
    # middleware grants owner rights to loopback callers, so the desktop window
    # must never load via 0.0.0.0 or its own LAN IP or we'd lock the owner out of
    # their own Settings. `bind_host` is what uvicorn listens on.
    from waikiki import auth
    host = config.HOST
    port = _pick_port(host, config.PORT)
    bind_host = "0.0.0.0" if (host == "127.0.0.1" and auth.share_lan_enabled()) else host

    # Headless mode (used to smoke-test the packaged binary without a display):
    # run the server in the foreground, no window.
    if os.environ.get("WAIKIKI_HEADLESS"):
        print(f"Waikiki (headless) on http://{host}:{port}")
        uvicorn.Server(
            uvicorn.Config(fastapi_app, host=bind_host, port=port, log_level="info")
        ).run()
        return

    import webview

    class DesktopApi:
        """Exposed to the page as window.pywebview.api — native Save/Open dialogs."""

        def open_url(self, url):
            """Open a URL in the system default browser/viewer (e.g. a PDF)."""
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}

        def save_wiki(self, slug):
            from waikiki import wikis
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=f"{wikis.name_of(slug)}.wiki")
            if not result:
                return {"ok": False, "cancelled": True}
            path = result if isinstance(result, str) else result[0]
            try:
                wikis.export_to(slug, path)
                return {"ok": True, "path": path}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def open_wiki(self):
            from waikiki import wikis
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Waikiki wiki (*.wiki;*.db)", "All files (*.*)"))
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            try:
                slug = wikis.import_from(path)
                return {"ok": True, "slug": slug, "name": wikis.name_of(slug)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    server = uvicorn.Server(
        uvicorn.Config(fastapi_app, host=bind_host, port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to accept connections before showing the window.
    deadline = time.time() + 40
    while time.time() < deadline and not _port_open(host, port):
        time.sleep(0.15)
    if not _port_open(host, port):
        print("Waikiki server failed to start", file=sys.stderr)
        sys.exit(1)

    # Native menu bar: gives macOS an Edit menu (so Copy/Paste/Select All exist as
    # real menu items — WKWebView has none by default) and a Help menu.
    base = f"http://{host}:{port}"

    def _js(code):
        def handler():
            try:
                webview.windows[0].evaluate_js(code)
            except Exception:
                pass
        return handler

    def _go(path):
        def handler():
            try:
                webview.windows[0].load_url(base + path)
            except Exception:
                pass
        return handler

    from webview.menu import Menu, MenuAction
    menu = [
        Menu("Edit", [
            MenuAction("Copy", _js("window.wkCopy && window.wkCopy()")),
            MenuAction("Paste", _js("window.wkPaste && window.wkPaste()")),
            MenuAction("Select All", _js("window.wkSelectAll && window.wkSelectAll()")),
        ]),
        Menu("Help", [
            MenuAction("Help Contents", _go("/help")),
            MenuAction("About Waikiki", _go("/help/about")),
            MenuAction("Connect Claude", _go("/connect")),
        ]),
    ]

    webview.create_window(
        "Waikiki", f"http://{host}:{port}/", width=1200, height=820,
        min_size=(800, 600), js_api=DesktopApi(), text_select=True,
    )
    webview.start(menu=menu)  # blocks until the window is closed

    server.should_exit = True  # ask uvicorn to stop; daemon thread exits with us


if __name__ == "__main__":
    main()
