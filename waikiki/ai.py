"""AI streaming into the wiki.

Streams Claude tokens over Server-Sent Events so the editor fills in live, and
grounds generations in the wiki's own content via hybrid RAG retrieval. This is
the "AI can stream into it" bonus.
"""
from __future__ import annotations

from typing import AsyncIterator, Optional

from . import config, rag

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic  # lazy: only needed when AI is used

        # Resolves ANTHROPIC_API_KEY / `ant auth login` profile automatically.
        _client = anthropic.AsyncAnthropic()
    return _client


def _build_context(query: str) -> str:
    hits = rag.search_chunks(query, k=config.RAG_TOP_K)
    if not hits:
        return ""
    blocks = [f"[from '{h['title']}']\n{h['text']}" for h in hits]
    return "\n\n---\n\n".join(blocks)


async def stream_completion(
    prompt: str, page_context: Optional[str] = None, use_rag: bool = True
) -> AsyncIterator[str]:
    """Yield text deltas from Claude. Consumed by the SSE endpoint."""
    client = _get_client()

    system = (
        "You are a writing assistant embedded in a local wiki. Produce clean "
        "GitHub-flavored Markdown (use tables where helpful). Return only the "
        "article content — no preamble, no code fences around the whole answer."
    )

    parts = []
    if use_rag:
        ctx = _build_context(prompt)
        if ctx:
            parts.append(
                "Relevant existing wiki content you may draw on:\n\n" + ctx
            )
    if page_context:
        parts.append("Current draft of the page:\n\n" + page_context)
    parts.append(prompt)
    user_message = "\n\n".join(parts)

    async with client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
