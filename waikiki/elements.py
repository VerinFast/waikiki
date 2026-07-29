"""User-defined custom elements (HTML5 Web Components) for structured content.

An element is invoked from wiki markup as a fenced block whose info string is the
element's slug:

    ```infobox
    title: Spider-Man
    Real name: Peter Parker
    Publisher: Marvel Comics
    ```

It renders to a `<wk-{slug}>` Web Component — Shadow DOM gives it a scoped CSS and
encapsulated JS. Elements declare fields; required ones must be present or the
block renders an inline error instead. Elements are per-wiki (stored in the
`custom_elements` table); the render pipeline lives here so the markup stays clean.
"""
from __future__ import annotations

import html as _html
import json
import re

from . import db

# Fenced block: ```<slug>\n <body> \n```   (slug = letters/digits/-/_ )
_FENCE = re.compile(r"(?ms)^```[ \t]*([A-Za-z0-9][A-Za-z0-9_-]*)[ \t]*\n(.*?)\n```[ \t]*$")
# Placeholder markdown-it wraps in <p>…</p>; swapped for the element after render.
_SLOT = re.compile(r"<p>WKELSLOT(\d+)ENDWKEL</p>")


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").strip().lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")


# --- CRUD (active wiki) -------------------------------------------------------

def list_elements() -> list[dict]:
    return [dict(r) for r in db.get_conn().execute(
        "SELECT slug, name, fields, html, css, js FROM custom_elements "
        "ORDER BY name COLLATE NOCASE").fetchall()]


def get_element(slug: str):
    r = db.get_conn().execute(
        "SELECT slug, name, fields, html, css, js FROM custom_elements WHERE slug=?",
        (slug,)).fetchone()
    return dict(r) if r else None


def parse_fields(text: str) -> list[dict]:
    """UI textarea format → schema. One field per line: `name` or `name*`
    (required), optionally `name* | Label`."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        namepart, _, label = line.partition("|")
        namepart = namepart.strip()
        required = namepart.endswith("*")
        fname = namepart.rstrip("*").strip()
        if fname:
            out.append({"name": fname, "required": required,
                        "label": label.strip() or fname})
    return out


def _normalize_fields(fields) -> list[dict]:
    """Accept a schema list (dicts) or the textarea string or a list of strings."""
    if isinstance(fields, str):
        return parse_fields(fields)
    out = []
    for f in fields or []:
        if isinstance(f, str):
            out.extend(parse_fields(f))
        elif isinstance(f, dict) and f.get("name"):
            out.append({"name": f["name"], "required": bool(f.get("required")),
                        "label": f.get("label") or f["name"]})
    return out


def fields_to_text(fields_json: str) -> str:
    try:
        fields = json.loads(fields_json or "[]")
    except Exception:
        return ""
    lines = []
    for f in fields:
        s = f.get("name", "") + ("*" if f.get("required") else "")
        if f.get("label") and f["label"] != f.get("name"):
            s += " | " + f["label"]
        lines.append(s)
    return "\n".join(lines)


def save_element(slug: str, name: str, fields, html: str, css: str, js: str) -> str:
    slug = slugify(slug or name)
    if not slug:
        raise ValueError("element needs a name")
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO custom_elements(slug, name, fields, html, css, js) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name, "
        "fields=excluded.fields, html=excluded.html, css=excluded.css, js=excluded.js",
        (slug, name or slug, json.dumps(_normalize_fields(fields)),
         html or "", css or "", js or ""))
    conn.commit()
    return slug


def delete_element(slug: str) -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM custom_elements WHERE slug=?", (slug,))
    conn.commit()


# --- Rendering ----------------------------------------------------------------

def registry() -> dict:
    return {e["slug"]: e for e in list_elements()}


def _parse_body(body: str) -> dict:
    props = {}
    for line in body.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            k = k.strip()
            if k:
                props[k] = v.strip()
    return props


def expand(body: str, reg: dict):
    """Swap element fences for placeholders (pre-markdown). Returns (body, slots)."""
    slots: list[dict] = []

    def repl(m):
        el = reg.get(m.group(1))
        if not el:
            return m.group(0)                  # ordinary code block — leave it
        props = _parse_body(m.group(2))
        try:
            fields = json.loads(el["fields"] or "[]")
        except Exception:
            fields = []
        missing = [f["name"] for f in fields
                   if f.get("required") and not (props.get(f["name"]) or "").strip()]
        slots.append({"el": el, "props": props, "missing": missing})
        return f"\n\nWKELSLOT{len(slots) - 1}ENDWKEL\n\n"

    return _FENCE.sub(repl, body), slots


def fill(html: str, slots: list):
    """Replace post-render placeholders with element tags (or errors)."""
    used: dict = {}

    def repl(m):
        slot = slots[int(m.group(1))]
        el = slot["el"]
        if slot["missing"]:
            return ('<div class="element-error">⚠️ <strong>'
                    f'{_html.escape(el["name"] or el["slug"])}</strong> — missing '
                    f'required field(s): {_html.escape(", ".join(slot["missing"]))}</div>')
        used[el["slug"]] = el
        data = _html.escape(json.dumps(slot["props"]), quote=True)
        tag = "wk-" + el["slug"]
        return f'<div class="wk-element-host"><{tag} data-props="{data}"></{tag}></div>'

    return _SLOT.sub(repl, html), used


def defs_script(used: dict) -> str:
    if not used:
        return ""
    defs = [{"tag": "wk-" + s, "html": e["html"] or "", "css": e["css"] or "",
             "js": e["js"] or ""} for s, e in used.items()]
    payload = json.dumps(defs).replace("</", "<\\/")   # safe inside <script>
    return f'<script class="wk-element-defs" type="application/json">{payload}</script>'
