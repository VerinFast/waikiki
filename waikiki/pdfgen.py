"""Render a wiki page to PDF with xhtml2pdf (pure-Python, bundles cleanly).

Images stored in the DB (served at /image/{id}) are resolved to bytes via a
link callback so they embed in the PDF.
"""
from __future__ import annotations

import io
import re
import tempfile

from . import store

_CSS = """
@page { margin: 1.6cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; color: #222; }
h1 { font-size: 20pt; } h2 { font-size: 15pt; } h3 { font-size: 12pt; }
h1, h2, h3, h4 { color: #111; }
a { color: #0969da; text-decoration: none; }
.header-anchor { display: none; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 4px 6px; }
th { background: #eee; }
pre { background: #f4f4f4; padding: 8px; }
code { background: #f4f4f4; font-family: monospace; }
img { max-width: 100%; }
blockquote { border-left: 3px solid #ccc; margin-left: 0; padding-left: 10px; color: #555; }
"""


def _link_callback(uri: str, rel: str) -> str:
    """Resolve /image/{id}[/name] URLs to a temp file so images embed."""
    m = re.match(r"/image/(\d+)", uri or "")
    if m:
        img = store.get_image(int(m.group(1)))
        if img:
            suffix = "." + (img["filename"].rsplit(".", 1)[-1] if "." in img["filename"] else "png")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(img["data"])
            tmp.close()
            return tmp.name
    return uri


def page_pdf(title: str, html_body: str) -> bytes:
    from xhtml2pdf import pisa  # lazy import (heavy-ish)

    doc = (f"<html><head><style>{_CSS}</style></head><body>"
           f"<h1>{title}</h1>{html_body}</body></html>")
    buf = io.BytesIO()
    pisa.CreatePDF(src=doc, dest=buf, link_callback=_link_callback)
    return buf.getvalue()
