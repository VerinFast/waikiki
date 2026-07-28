"""Built-in Help wiki content.

Help lives in its own isolated wiki (slug ``help``) so it's browsable like any
other content and the AI system prompts we pass are editable wiki pages. Seeding
is idempotent: prose pages are created once (so user edits stick), while the
About page is re-stamped with the current version on every start.
"""
from __future__ import annotations

from . import ai, chat, config, db, render, store, wikis


def _home() -> str:
    return (
        "# Getting Started\n\n"
        "Welcome to **Waikiki** — a small local wiki for Human/LLM collaboration.\n\n"
        "- [[Editing & Formatting]] — markdown, tables, wikilinks, sections, "
        "properties\n"
        "- [[Templates]] — reusable page skeletons\n"
        "- [[Wikis & Isolation]] — multiple isolated wikis, switching, save/open\n"
        "- [[AI & Chat]] — generate drafts, and chat with an article\n"
        "- [[Connect Claude]] — let Claude Desktop read & edit your wikis over MCP\n"
        "- [[About]] — version, repository, license\n\n"
        "> This Help wiki is itself editable. The two *System Prompt* pages below "
        "control what the AI is told — tune them to taste.\n\n"
        "- [[Generation System Prompt]]\n"
        "- [[Chat System Prompt]]\n"
    )


def _editing() -> str:
    return (
        "# Editing & Formatting\n\n"
        "Pages are written in GitHub-flavored Markdown and rendered to HTML.\n\n"
        "## Links\n"
        "- `[[Page Title]]` links to another page **in this wiki** (links never "
        "cross wikis).\n"
        "- `[[Page Title|label]]` sets the link text.\n"
        "- `[[Page#Section]]` jumps to a heading; `[[#Section]]` is same-page.\n\n"
        "## Sections\n"
        "Every heading gets an anchor and a small **edit** link (view mode) so you "
        "can edit just that section. The **Contents** box links to each heading.\n\n"
        "## Tables\n"
        "Standard `| a | b |` tables render, and each cell has a pencil for a quick "
        "in-place edit while viewing.\n\n"
        "## Properties\n"
        "Add a YAML front-matter block to store values and show an infobox:\n\n"
        "```\n---\nHitPoints: 42\nRole: Guardian\n---\n```\n\n"
        "Reference them with `{{HitPoints}}` (this page) or `{{Meru.HitPoints}}` "
        "(another page in the same wiki).\n\n"
        "## Media & transclusion\n"
        "Drag or attach images, video, and audio in the editor. Embed another "
        "page with `![[Page Title]]`.\n"
    )


def _templates() -> str:
    return (
        "# Templates\n\n"
        "Templates are reusable page skeletons — start a new page from one instead "
        "of a blank editor.\n\n"
        "## Using a template\n"
        "Click **+ New**, then pick a template (or open `/new?template=<name>`). "
        "The template's markdown pre-fills the editor; edit and save as usual.\n\n"
        "## Placeholders\n"
        "`{{title}}` in a template is replaced with the new page's title when you "
        "create from it. (Other `{{Key}}` values interpolate from the page's own "
        "frontmatter once saved — see [[Editing & Formatting]].)\n\n"
        "## Built-in templates\n"
        "- **Meeting notes** — date, attendees, agenda, notes, action items.\n"
        "- **How-to** — summary, prerequisites, numbered steps, see-also.\n"
        "- **Person** — an infobox (role/team/contact) plus About and Notes.\n\n"
        "## Managing templates\n"
        "**Settings → Templates** lists them; add or edit one there (name + "
        "markdown body). Deleting a template never touches pages created from it.\n\n"
        "## For Claude (MCP)\n"
        "- `list_templates` — see available templates.\n"
        "- `create_template(name, markdown)` — add or replace one.\n"
        "- `create_from_template(name, title)` — make a page from a template.\n"
    )


def _wikis() -> str:
    return (
        "# Wikis & Isolation\n\n"
        "Waikiki holds several **isolated** wikis — pages in one can never link to "
        "or surface pages in another, because each wiki is a separate database.\n\n"
        "## Switching\n"
        "Use the wiki selector in the top bar. Claude (over MCP) has its *own* "
        "active wiki and must call `switch_wiki` to move — this keeps contexts "
        "from mixing.\n\n"
        "## Save & Open\n"
        "From **Manage wikis** (the ⚙ by the selector) you can export a wiki to a "
        "`.wiki` file (a zip of the database + media) and open one back up. In the "
        "desktop app these use native Save/Open dialogs.\n"
    )


def _ai() -> str:
    return (
        "# AI & Chat\n\n"
        "## Generate (stream into the editor)\n"
        "In the editor, **Generate** streams a draft in live. Pick the provider in "
        "**Settings → AI generation**:\n\n"
        "- **Anthropic** (cloud) — uses your `ANTHROPIC_API_KEY` / `ant` login.\n"
        "- **Ollama** (local) — a model like `phi3` running on your machine. "
        "Install [Ollama](https://ollama.com), run `ollama serve`, then "
        "`ollama pull phi3`.\n\n"
        "Generations are grounded in this wiki's own content via hybrid (BM25 + "
        "vector) retrieval.\n\n"
        "## Chat with an article\n"
        "Open any page and use the **Chat** panel to ask questions about it. The "
        "answer is grounded in the page plus related excerpts from the same wiki, "
        "and is produced by a local CLI you choose in **Settings → Chat**:\n\n"
        "- **Claude** — the Claude Code CLI (`npm i -g @anthropic-ai/claude-code`).\n"
        "- **Gemini** — Google's Gemini CLI (`npm i -g @google/gemini-cli`).\n\n"
        "The exact instructions the AI receives live in [[Generation System Prompt]] "
        "and [[Chat System Prompt]] — edit them to change its behavior.\n"
    )


def _connect() -> str:
    return (
        "# Connect Claude\n\n"
        "Let Claude Desktop read and edit your wikis over MCP, and co-edit pages "
        "with you live.\n\n"
        "Open **the interactive setup page** at `/connect` (also under the Help "
        "menu) — it shows the exact Claude Desktop config for *this* install with "
        "real paths filled in, ready to copy-paste.\n\n"
        "After connecting, tell Claude to `list_wikis` and `switch_wiki` before it "
        "reads or writes — every tool result names the wiki it acted on.\n"
    )


def _about(version: str) -> str:
    return (
        "# About\n\n"
        f"**Waikiki** version **{version}**.\n\n"
        "A local, SQLite-backed wiki for Human/LLM collaboration — markdown, "
        "hybrid RAG search, live CRDT co-editing, and an MCP server for Claude.\n\n"
        "| | |\n|---|---|\n"
        f"| Version | {version} |\n"
        "| Repository | https://github.com/VerinFast/waikiki |\n"
        "| License | To be determined (not yet released open source) |\n\n"
        "If we open-source Waikiki, license and contribution details will live "
        "here.\n"
    )


def _upsert(title: str, body: str, *, overwrite: bool) -> None:
    slug = render.slugify(title)
    page = store.get_page(slug)
    if page is None:
        store.create_page(title, body, author="system")
    elif overwrite and page.get("markdown") != body:
        store.update_page(slug, title, body, author="system")


def seed() -> None:
    """Register the Help wiki and (idempotently) populate its pages."""
    wikis.ensure_help_wiki()
    token = db.current_wiki.set(config.HELP_WIKI)
    try:
        db.get_conn()  # ensure schema for the help DB
        _upsert("Getting Started", _home(), overwrite=False)
        _upsert("Editing & Formatting", _editing(), overwrite=False)
        _upsert("Templates", _templates(), overwrite=False)
        _upsert("Wikis & Isolation", _wikis(), overwrite=False)
        _upsert("AI & Chat", _ai(), overwrite=False)
        _upsert("Connect Claude", _connect(), overwrite=False)
        # System prompts: created once, then owned by the user (never overwritten).
        _upsert("Generation System Prompt", ai.DEFAULT_GEN_SYSTEM, overwrite=False)
        _upsert("Chat System Prompt", chat.DEFAULT_CHAT_SYSTEM, overwrite=False)
        # About is app-owned: keep the version current.
        _upsert("About", _about(config.VERSION), overwrite=True)
    finally:
        db.current_wiki.reset(token)
