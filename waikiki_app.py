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

    host = config.HOST
    port = _pick_port(host, config.PORT)

    # Headless mode (used to smoke-test the packaged binary without a display):
    # run the server in the foreground, no window.
    if os.environ.get("WAIKIKI_HEADLESS"):
        print(f"Waikiki (headless) on http://{host}:{port}")
        uvicorn.Server(
            uvicorn.Config(fastapi_app, host=host, port=port, log_level="info")
        ).run()
        return

    import webview

    class DesktopApi:
        """Exposed to the page as window.pywebview.api — native Save/Open dialogs."""

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
        uvicorn.Config(fastapi_app, host=host, port=port, log_level="warning")
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

    webview.create_window(
        "Waikiki", f"http://{host}:{port}/", width=1200, height=820,
        min_size=(800, 600), js_api=DesktopApi(),
    )
    webview.start()  # blocks until the window is closed

    server.should_exit = True  # ask uvicorn to stop; daemon thread exits with us


if __name__ == "__main__":
    main()
