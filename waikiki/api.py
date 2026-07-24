"""FastAPI app: HTML wiki views + JSON REST API + image serving + AI SSE stream
+ real-time collaborative editing (CRDT) at /collab."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (FastAPI, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (HTMLResponse, RedirectResponse,
                               StreamingResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import ai, collab, config, db, rag, render, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Pre-load the embedding model once, off the event loop, so the first search
    # and a concurrent reindex don't both try to cold-load it and race.
    async def _warm():
        try:
            import anyio
            from . import embeddings
            await anyio.to_thread.run_sync(lambda: embeddings.get_embedder().embed(["warmup"]))
        except Exception as exc:
            print(f"[waikiki] embedder warmup skipped: {exc}")

    async with collab.server:                       # start the CRDT websocket server
        warm_task = asyncio.create_task(_warm())
        flush_task = asyncio.create_task(collab.flusher())
        try:
            yield
        finally:
            warm_task.cancel()
            flush_task.cancel()


app = FastAPI(title="Waikiki", version="0.1.0", lifespan=lifespan)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class _StarletteChannel:
    """Adapts a Starlette WebSocket to the pycrdt-websocket Channel protocol.

    The room name is `path` — we pass the bare slug so it matches the room that
    ensure_room()/append/replace operate on (a plain Mount would rewrite the
    path to '/<slug>' and desync into a different, empty room)."""

    def __init__(self, websocket: WebSocket, path: str):
        self._ws = websocket
        self.path = path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._ws.receive_bytes()
        except WebSocketDisconnect:
            raise StopAsyncIteration

    async def send(self, message: bytes) -> None:
        await self._ws.send_bytes(message)

    async def recv(self) -> bytes:
        return await self._ws.receive_bytes()


@app.websocket("/collab/{slug}")
async def collab_ws(websocket: WebSocket, slug: str):
    await websocket.accept()
    await collab.ensure_room(slug)  # seed from DB before serving
    channel = _StarletteChannel(websocket, slug)
    try:
        await collab.server.serve(channel)
    except WebSocketDisconnect:
        pass


def _ctx(request: Request, **extra) -> dict:
    """Common template context: active theme, nav pages, pygments styles."""
    base = {
        "request": request,
        "theme": db.get_setting("theme", "default"),
        "nav_pages": store.list_pages()[:50],
        "pygments_css": render.pygments_css(),
        "vec_available": db.VEC_AVAILABLE,
    }
    base.update(extra)
    return base


# =============================================================================
# HTML views (human-facing)
# =============================================================================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request,
        "index.html", _ctx(request, pages=store.list_pages())
    )


@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request):
    return templates.TemplateResponse(request,
        "edit.html",
        _ctx(request, page={"slug": "", "title": "", "markdown": ""}, is_new=True),
    )


@app.get("/wiki/{slug}", response_class=HTMLResponse)
def view_page(request: Request, slug: str):
    page = store.get_page(slug)
    if not page:
        # Offer to create it (wiki-style red link).
        return templates.TemplateResponse(request,
            "edit.html",
            _ctx(request, page={"slug": "", "title": slug.replace("-", " ").title(),
                                "markdown": ""}, is_new=True, missing=slug),
            status_code=404,
        )
    return templates.TemplateResponse(request,
        "page.html", _ctx(request, page=page, versions=store.page_versions(slug))
    )


@app.get("/wiki/{slug}/edit", response_class=HTMLResponse)
async def edit_page(request: Request, slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    # Seed the CRDT room from the DB before the browser's websocket connects,
    # so the live document already has the page content.
    await collab.ensure_room(slug)
    return templates.TemplateResponse(request,
        "edit.html", _ctx(request, page=page, is_new=False, collab=True)
    )


@app.post("/wiki/save", response_class=HTMLResponse)
def save_page(slug: str = Form(""), title: str = Form(...), markdown: str = Form("")):
    if slug:
        page = store.update_page(slug, title, markdown, author="human")
    else:
        page = store.create_page(title, markdown, author="human")
    return RedirectResponse(f"/wiki/{page['slug']}", status_code=303)


@app.post("/wiki/{slug}/delete")
def delete_page_view(slug: str):
    store.delete_page(slug)
    return RedirectResponse("/", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_view(request: Request, q: str = ""):
    results = rag.search_pages(q) if q else []
    return templates.TemplateResponse(request,
        "search.html", _ctx(request, q=q, results=results)
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request):
    themes = sorted(p.stem for p in (_STATIC / "themes").glob("*.css"))
    return templates.TemplateResponse(request,
        "settings.html",
        _ctx(request, settings=db.all_settings(), themes=themes),
    )


@app.post("/settings")
def settings_save(
    theme: str = Form(...),
    embedder_provider: str = Form(...),
    embedder_local_model: str = Form(...),
    embedder_voyage_model: str = Form(...),
    reindex: str = Form(""),
):
    prev = (db.get_setting("embedder_provider"), db.get_setting("embedder_local_model"),
            db.get_setting("embedder_voyage_model"))
    db.set_setting("theme", theme)
    db.set_setting("embedder_provider", embedder_provider)
    db.set_setting("embedder_local_model", embedder_local_model)
    db.set_setting("embedder_voyage_model", embedder_voyage_model)
    now = (embedder_provider, embedder_local_model, embedder_voyage_model)
    if reindex or prev != now:
        rag.reindex_all()  # embedder changed → rebuild vectors
    return RedirectResponse("/settings", status_code=303)


@app.get("/image/{image_id}")
def serve_image(image_id: int):
    img = store.get_image(image_id)
    if not img:
        raise HTTPException(404, "Image not found")
    return Response(content=img["data"], media_type=img["mimetype"])


# =============================================================================
# JSON REST API (LLM / programmatic)
# =============================================================================

class PageIn(BaseModel):
    title: str
    markdown: str = ""


@app.get("/api/pages")
def api_list_pages():
    return store.list_pages()


@app.post("/api/pages")
def api_create_page(body: PageIn):
    return store.create_page(body.title, body.markdown, author="api")


@app.get("/api/pages/{slug}")
def api_get_page(slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    return page


@app.put("/api/pages/{slug}")
def api_update_page(slug: str, body: PageIn):
    page = store.update_page(slug, body.title, body.markdown, author="api")
    if not page:
        raise HTTPException(404, "Page not found")
    return page


@app.delete("/api/pages/{slug}")
def api_delete_page(slug: str):
    if not store.delete_page(slug):
        raise HTTPException(404, "Page not found")
    return {"deleted": slug}


@app.get("/api/search")
def api_search(q: str, k: int = config.RAG_TOP_K):
    """Hybrid BM25 + vector retrieval over the wiki (the RAG endpoint)."""
    return {"query": q, "results": rag.search_chunks(q, k)}


@app.post("/api/images")
async def api_upload_image(file: UploadFile):
    data = await file.read()
    image_id = store.save_image(file.filename or "image",
                                file.content_type or "application/octet-stream", data)
    url = f"/image/{image_id}"
    return {"id": image_id, "url": url,
            "markdown": f"![{file.filename or 'image'}]({url})"}


class AIRequest(BaseModel):
    prompt: str
    page_context: str | None = None
    use_rag: bool = True


@app.post("/api/ai/stream")
async def api_ai_stream(body: AIRequest):
    """Stream Claude tokens as SSE. The editor appends them live."""
    async def event_gen():
        try:
            async for delta in ai.stream_completion(
                body.prompt, body.page_context, body.use_rag
            ):
                yield f"data: {json.dumps({'text': delta})}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as exc:  # surface errors to the client
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# =============================================================================
# Collaboration injection (called by the MCP server so Claude writes live)
# =============================================================================

class CollabAppend(BaseModel):
    text: str


class CollabReplace(BaseModel):
    markdown: str


@app.post("/api/collab/{slug}/append")
async def api_collab_append(slug: str, body: CollabAppend):
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    text = await collab.append_text(slug, body.text)
    return {"slug": slug, "length": len(text)}


@app.post("/api/collab/{slug}/replace")
async def api_collab_replace(slug: str, body: CollabReplace):
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    text = await collab.replace_text(slug, body.markdown)
    return {"slug": slug, "length": len(text)}


@app.get("/api/collab/{slug}/live")
async def api_collab_live(slug: str):
    """Current live (possibly unsaved) markdown for a page."""
    md = await collab.live_markdown(slug)
    if md is None:
        page = store.get_page(slug)
        if not page:
            raise HTTPException(404, "Page not found")
        md = page["markdown"]
    return {"slug": slug, "markdown": md}
