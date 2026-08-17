"""Ask an agent to draft a template or a custom element.

The MCP surface is already the backend — `create_template` and `create_element`
exist as tools — so what was missing was a way to ask from the app. This is that
path, and it deliberately stops short of saving.

**The agent is read-only and returns content; the form saves it.** Elements ship
arbitrary HTML and JS into every page that uses them, so "an agent wrote this"
and "this is live in my wiki" stay two separate steps, with a human reading the
result in the editor in between. That is why this does not simply grant
`create_element` and let the agent write.

Runs the same CLI path as chat (`--mcp-config`, `--strict-mcp-config`, an
allow-list of read-only tools), so the agent can read the wiki's existing
elements and documentation and match the house style rather than inventing its
own idiom.
"""
from __future__ import annotations

import json
import re
import subprocess

from . import chat, clirun, db, elements, store

TIMEOUT = 240

_ELEMENT_RULES = """\
A Waikiki custom element is a Web Component rendered in a shadow root.

- `fields` is one per line, `name | Label`, with a `*` after the name for
  required: `title* | Title`. Required fields missing on a page render an error
  block instead of the component.
- `html` is the component's markup. `{{field}}` interpolates a field's value and
  is already escaped, with [[wiki links]] resolved.
- `css` is scoped to the shadow root, so plain selectors are safe and cannot
  leak into the page.
- `js` runs with `root` (the shadow root), `props` (raw field values), `html`
  (the same values rendered, links resolved) and `host`. Assigning `props` with
  textContent is fine — the runtime sweeps the shadow DOM afterwards and turns
  any remaining [[link]] into a real link, so components do not need their own
  link parser.
"""

_TEMPLATE_RULES = """\
A Waikiki template is a markdown skeleton for new pages. It may open with a
`---` frontmatter block declaring properties, and may use ```fenced blocks that
name a custom element to place one.
"""


def _prompt(kind: str, description: str, wiki: str) -> str:
    rules = _ELEMENT_RULES if kind == "element" else _TEMPLATE_RULES
    shape = ('{"name": "...", "fields": "...", "html": "...", "css": "...", '
             '"js": "..."}' if kind == "element" else '{"name": "...", "markdown": "..."}')
    return "\n\n---\n\n".join([
        f"You are drafting a Waikiki {kind} for the '{wiki}' wiki.",
        rules,
        ("You can read this wiki with the tools you have. Look at the existing "
         "elements and the built-in docs (list_elements, get_element, list_docs, "
         f"read_doc) so this {kind} matches how the others are written, rather "
         "than a generic idiom. Call switch_wiki(\"" + wiki + "\") first — a "
         "fresh session has no active wiki and the tools will refuse until you "
         "do."),
        f"# What is wanted\n\n{description.strip()}",
        ("# Reply format\n\nReply with ONE JSON object and nothing else — no "
         f"prose, no code fence:\n\n{shape}"),
    ])


def _extract_json(out: str) -> dict | None:
    """Pull the JSON object out of a CLI's reply.

    Models wrap JSON in fences or a sentence often enough that being strict here
    just turns a good draft into an error the user cannot act on.
    """
    text = re.sub(r"\x1b\[[0-9;]*m", "", out or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def draft(kind: str, description: str, provider: str = "claude",
          model: str = "", timeout: int = TIMEOUT) -> dict:
    """Draft a template or element. Returns {ok, fields...} or {ok: False, error}.

    Never saves anything — the caller puts the result in the form.
    """
    if kind not in ("element", "template"):
        return {"ok": False, "error": f"unknown kind '{kind}'"}
    if not (description or "").strip():
        return {"ok": False, "error": "Describe what you want first."}

    provider = provider if provider in ("claude", "gemini") else "claude"
    binary = "gemini" if provider == "gemini" else "claude"
    cli = chat.find_cli(binary)
    if not cli:
        return {"ok": False,
                "error": f"`{binary}` CLI not found on this machine. Install it, "
                         f"then reopen Waikiki."}

    prompt = _prompt(kind, description, db.active_wiki())
    try:
        proc = clirun.run(f"{binary}:draft-{kind}",
                          chat._cli_args(provider, cli, model, prompt), timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"The {binary} CLI timed out after {timeout}s."}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to run {binary}: {exc}"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": f"{binary} error: {err[:400]}"}

    data = _extract_json(out)
    if not data:
        return {"ok": False,
                "error": "The agent did not reply with usable JSON. Try again, "
                         "or describe it more specifically."}

    if kind == "element":
        return {"ok": True, "kind": kind,
                "name": str(data.get("name") or "").strip(),
                "fields": str(data.get("fields") or "").strip(),
                "html": str(data.get("html") or "").strip(),
                "css": str(data.get("css") or "").strip(),
                "js": str(data.get("js") or "").strip()}
    return {"ok": True, "kind": kind,
            "name": str(data.get("name") or "").strip(),
            "markdown": str(data.get("markdown") or "").strip()}
