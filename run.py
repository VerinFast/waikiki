#!/usr/bin/env python3
"""Launch the Waikiki web app.

    python run.py           # serves http://127.0.0.1:8787
"""
import uvicorn

from waikiki import config

if __name__ == "__main__":
    uvicorn.run("waikiki.api:app", host=config.HOST, port=config.PORT, reload=False)
