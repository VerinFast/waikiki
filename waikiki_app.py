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
        "Waikiki", f"http://{host}:{port}/", width=1200, height=820, min_size=(800, 600)
    )
    webview.start()  # blocks until the window is closed

    server.should_exit = True  # ask uvicorn to stop; daemon thread exits with us


if __name__ == "__main__":
    main()
