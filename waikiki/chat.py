"""Chat with an article, powered by a local CLI (Claude Code or Gemini).

The user asks a question about a page; we ground the answer in (a) the page's
own markdown and (b) hybrid-RAG excerpts from elsewhere in the *same* wiki, then
shell out to the `claude` or `gemini` CLI to answer. The CLI runs in one-shot
"print" mode — no interactive session — so this stays a simple request/response.

The system prompt is an editable page in the Help wiki, so it can be tuned
without code changes. Nothing here crosses wiki boundaries: retrieval always
runs under the caller's active wiki.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config, rag, store

DEFAULT_CHAT_SYSTEM = (
    "You are a helpful assistant answering questions about a single wiki article. "
    "Ground every answer in the article and the provided wiki excerpts. If the "
    "answer isn't in them, say so plainly rather than guessing. Be concise and "
    "use Markdown."
)
CHAT_PROMPT_SLUG = "chat-system-prompt"

# Where CLIs commonly live when the login shell PATH isn't inherited (packaged
# .app launched from Finder gets a minimal PATH).
_EXTRA_BINS = [
    Path.home() / ".claude" / "local",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path.home() / ".npm-global" / "bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path.home() / ".volta" / "bin",
]


def find_cli(name: str) -> str | None:
    """Locate a CLI binary by name — PATH first, then common install dirs."""
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BINS:
        cand = d / name
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def chat_system_prompt() -> str:
    from . import ai
    return ai.help_prompt(CHAT_PROMPT_SLUG) or DEFAULT_CHAT_SYSTEM


def build_prompt(title: str, article_md: str, excerpts: str,
                 history: list[dict], question: str, system: str) -> str:
    """Assemble the single prompt string handed to the CLI. Pure — unit-tested."""
    parts = [system, f"# Article: {title}\n\n{article_md.strip()}"]
    if excerpts.strip():
        parts.append("# Other relevant excerpts from this wiki\n\n" + excerpts.strip())
    convo = []
    for turn in history or []:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = (turn.get("content") or "").strip()
        if text:
            convo.append(f"{role}: {text}")
    if convo:
        parts.append("# Conversation so far\n\n" + "\n\n".join(convo))
    parts.append(f"# New question\n\n{question.strip()}")
    return "\n\n---\n\n".join(parts)


def _cli_args(provider: str, cli: str, model: str, prompt: str) -> list[str]:
    """Argv for one-shot print mode. Claude Code: `-p`; Gemini: `-p`, `-m`."""
    if provider == "gemini":
        args = [cli, "-p", prompt]
        if model:
            args += ["-m", model]
    else:  # claude (default)
        args = [cli, "-p", prompt]
        if model:
            args += ["--model", model]
    return args


def _excerpts(question: str, exclude_slug: str) -> str:
    try:
        hits = rag.search_chunks(question, k=config.RAG_TOP_K)
    except Exception:
        return ""
    blocks = [f"[from '{h['title']}']\n{h['text']}"
              for h in hits if h.get("slug") != exclude_slug]
    return "\n\n---\n\n".join(blocks[:5])


def _run_env() -> dict:
    env = dict(os.environ)
    extra = os.pathsep.join(str(d) for d in _EXTRA_BINS if d.exists())
    if extra:
        env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    return env


def answer(slug: str, question: str, provider: str = "claude",
           model: str = "", history: list[dict] | None = None,
           timeout: int = 180) -> dict:
    """Answer a question about page `slug` using the selected CLI. Synchronous
    (subprocess) — call it in a worker thread from async code."""
    provider = provider if provider in ("claude", "gemini") else "claude"
    if not (question or "").strip():
        return {"ok": False, "error": "Ask a question first."}
    page = store.get_page(slug)
    if not page:
        return {"ok": False, "error": f"No page '{slug}'."}

    binary = "gemini" if provider == "gemini" else "claude"
    cli = find_cli(binary)
    if not cli:
        hint = ("Install Google's Gemini CLI (`npm i -g @google/gemini-cli`)"
                if provider == "gemini" else
                "Install the Claude Code CLI (`npm i -g @anthropic-ai/claude-code`)")
        return {"ok": False,
                "error": f"`{binary}` CLI not found on this machine. {hint}, "
                         f"then reopen Waikiki."}

    prompt = build_prompt(
        page["title"], page["markdown"], _excerpts(question, slug),
        history or [], question, chat_system_prompt())
    try:
        proc = subprocess.run(
            _cli_args(provider, cli, model, prompt),
            capture_output=True, text=True, timeout=timeout, env=_run_env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"The {binary} CLI timed out after {timeout}s."}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to run {binary}: {exc}"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": f"{binary} error: {err[:400]}"}
    # Some CLIs prepend a stray login/banner line; strip common ANSI noise.
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    return {"ok": True, "provider": provider, "answer": out}
