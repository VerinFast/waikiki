#!/usr/bin/env python3
"""Launch the Waikiki web app.

    python run.py           # serves http://127.0.0.1:8787

Binds beyond loopback only when LAN sharing is enabled *and* a password is set
(Settings → Share on your network) — the same rule the packaged app uses.
"""
import uvicorn

from waikiki import auth, config

if __name__ == "__main__":
    bind = config.HOST
    if bind == "127.0.0.1" and auth.share_lan_enabled():
        bind = "0.0.0.0"
    print(f"Waikiki on http://{config.HOST}:{config.PORT}"
          f"{'  (also serving the local network)' if bind == '0.0.0.0' else ''}")
    uvicorn.run("waikiki.api:app", host=bind, port=config.PORT, reload=False)
