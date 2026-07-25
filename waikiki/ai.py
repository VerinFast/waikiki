"""AI streaming into the wiki.

Streams tokens over Server-Sent Events so the editor fills in live, grounded in
the wiki's own content via hybrid RAG retrieval. Generation can run against
Anthropic (cloud, default) or a local Ollama model (e.g. phi3) — chosen in
Settings. The system prompt is an editable page in the Help wiki, so users can
tune it without touching code.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

from . import config, rag

_client = None

# Fallback system prompt, used if the Help wiki's editable copy is missing.
DEFAULT_GEN_SYSTEM = (
    "You are a writing assistant embedded in a local wiki. Produce clean "
    "GitHub-flavored Markdown (use tables where helpful). Return only the "
    "article content — no preamble, no code fences around the whole answer."
)

# Slug of the Help-wiki page that holds the editable generation system prompt.
GEN_PROMPT_SLUG = "generation-system-prompt"


def _get_client():
    global _client
    if _client is None:
        import anthropic  # lazy: only needed when AI is used

        # Resolves ANTHROPIC_API_KEY / `ant auth login` profile automatically.
        _client = anthropic.AsyncAnthropic()
    return _client


def _setting(key: str, default: str) -> str:
    from . import db
    return db.get_setting(key, default)


def help_prompt(slug: str) -> Optional[str]:
    """Return the body (H1 stripped) of a page in the Help wiki, or None. Used to
    surface user-editable system prompts stored as ordinary wiki pages."""
    import re

    from . import db, store, wikis
    if not wikis.exists(config.HELP_WIKI):
        return None
    token = db.current_wiki.set(config.HELP_WIKI)
    try:
        page = store.get_page(slug)
    except Exception:
        page = None
    finally:
        db.current_wiki.reset(token)
    if not page or not page.get("markdown"):
        return None
    body = re.sub(r"^#\s+.*\n+", "", page["markdown"], count=1).strip()
    return body or None


def generation_system_prompt() -> str:
    return help_prompt(GEN_PROMPT_SLUG) or DEFAULT_GEN_SYSTEM


def _build_context(query: str) -> str:
    hits = rag.search_chunks(query, k=config.RAG_TOP_K)
    if not hits:
        return ""
    blocks = [f"[from '{h['title']}']\n{h['text']}" for h in hits]
    return "\n\n---\n\n".join(blocks)


def _assemble_user(prompt: str, page_context: Optional[str], use_rag: bool) -> str:
    parts = []
    if use_rag:
        ctx = _build_context(prompt)
        if ctx:
            parts.append("Relevant existing wiki content you may draw on:\n\n" + ctx)
    if page_context:
        parts.append("Current draft of the page:\n\n" + page_context)
    parts.append(prompt)
    return "\n\n".join(parts)


async def _anthropic_stream(system: str, user_message: str) -> AsyncIterator[str]:
    client = _get_client()
    async with client.messages.stream(
        model=_setting("gen_model", config.ANTHROPIC_MODEL),
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def _ollama_stream(system: str, user_message: str) -> AsyncIterator[str]:
    """Stream tokens from a local Ollama server (NDJSON). Raises a friendly error
    if Ollama isn't reachable so the editor can show what to install."""
    import httpx

    url = _setting("ollama_url", "http://localhost:11434").rstrip("/")
    model = _setting("gen_model_local", "phi3")
    payload = {"model": model, "prompt": user_message, "system": system,
               "stream": True}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
            async with client.stream("POST", url + "/api/generate", json=payload) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")[:300]
                    raise RuntimeError(
                        f"Ollama returned {r.status_code}: {detail}. "
                        f"Is the model pulled? Try `ollama pull {model}`.")
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("response"):
                        yield obj["response"]
                    if obj.get("done"):
                        break
    except httpx.ConnectError:
        raise RuntimeError(
            f"Can't reach Ollama at {url}. Install it from ollama.com, run "
            f"`ollama serve`, then `ollama pull {model}` — or switch generation "
            f"back to Anthropic in Settings.")


async def stream_completion(
    prompt: str, page_context: Optional[str] = None, use_rag: bool = True
) -> AsyncIterator[str]:
    """Yield text deltas from the configured generation provider. Consumed by the
    SSE endpoint; the editor appends them live."""
    provider = _setting("gen_provider", "anthropic")
    system = generation_system_prompt()
    user_message = _assemble_user(prompt, page_context, use_rag)
    stream = _ollama_stream if provider == "ollama" else _anthropic_stream
    async for text in stream(system, user_message):
        yield text
