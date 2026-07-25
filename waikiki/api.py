"""FastAPI app: HTML wiki views + JSON REST API + image serving + AI SSE stream
+ real-time collaborative editing (CRDT) at /collab."""
from __future__ import annotations

import asyncio
import difflib
import json
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (FastAPI, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               StreamingResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import ai, collab, config, db, edits, embeddings, pdfgen, rag, render, store, wikis


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # ensures the wiki registry + default wiki schema
    # Pre-load the embedding model once (under the default wiki context), off the
    # event loop, so the first search and a concurrent reindex don't race to load.
    async def _warm():
        try:
            import anyio
            db.current_wiki.set(wikis.default_slug())
            await anyio.to_thread.run_sync(lambda: embeddings.get_embedder().embed(["warmup"]))
        except Exception as exc:
            print(f"[waikiki] embedder warmup skipped: {exc}")

    async def _retention():
        # Enforce the trash retention policy across all wikis, hourly.
        import anyio
        while True:
            for w in wikis.list_wikis():
                db.current_wiki.set(w["slug"])
                try:
                    await anyio.to_thread.run_sync(store.sweep_trash)
                except Exception as exc:
                    print(f"[waikiki] trash sweep failed for {w['slug']}: {exc}")
            await asyncio.sleep(3600)

    async with collab.server:                       # start the CRDT websocket server
        warm_task = asyncio.create_task(_warm())
        flush_task = asyncio.create_task(collab.flusher())
        retention_task = asyncio.create_task(_retention())
        try:
            yield
        finally:
            warm_task.cancel()
            flush_task.cancel()
            retention_task.cancel()


app = FastAPI(title="Waikiki", version="0.1.0", lifespan=lifespan)


def _resolve_wiki(scope) -> str:
    """Pick the active wiki for a request: /collab/{wiki}/... path segment, then
    the X-Waikiki-Wiki header (MCP), then the waikiki_wiki cookie (browser)."""
    path = scope.get("path", "") or ""
    if path.startswith("/collab/"):
        parts = path.split("/")
        if len(parts) >= 3 and parts[2] and wikis.exists(parts[2]):
            return parts[2]
    from urllib.parse import parse_qs
    q = parse_qs(scope.get("query_string", b"").decode())
    if q.get("wiki") and wikis.exists(q["wiki"][0]):
        return q["wiki"][0]
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    cand = headers.get("x-waikiki-wiki")
    if cand and wikis.exists(cand):
        return cand
    for part in headers.get("cookie", "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k == "waikiki_wiki" and wikis.exists(v):
                return v
    return wikis.default_slug() or "main"


class WikiContextMiddleware:
    """Pure-ASGI middleware: bind db.current_wiki for the whole request in one
    task, so the contextvar reaches sync endpoints (a BaseHTTPMiddleware would
    run downstream in a separate task and lose it)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            token = db.current_wiki.set(_resolve_wiki(scope))
            try:
                await self.app(scope, receive, send)
            finally:
                db.current_wiki.reset(token)
        else:
            await self.app(scope, receive, send)


app.add_middleware(WikiContextMiddleware)

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


@app.websocket("/collab/{wiki}/{slug}")
async def collab_ws(websocket: WebSocket, wiki: str, slug: str):
    if not wikis.exists(wiki):
        await websocket.close(code=4004)
        return
    await websocket.accept()
    db.current_wiki.set(wiki)
    await collab.ensure_room(wiki, slug)  # seed from the wiki's DB before serving
    channel = _StarletteChannel(websocket, collab.room_key(wiki, slug))
    try:
        await collab.server.serve(channel)
    except WebSocketDisconnect:
        pass


def _ctx(request: Request, **extra) -> dict:
    """Common template context: active wiki, theme, nav pages, pygments styles."""
    wiki = db.active_wiki()
    nav_filter = request.cookies.get("waikiki_nav_filter", "all")
    nav_sort = request.cookies.get("waikiki_nav_sort", "updated")
    base = {
        "request": request,
        "theme": db.get_setting("theme", "default"),
        "nav_pages": store.list_pages(sort=nav_sort,
                                      starred_only=(nav_filter == "starred"))[:500],
        "nav_filter": nav_filter,
        "nav_sort": nav_sort,
        "current_path": request.url.path,
        "pygments_css": render.pygments_css(),
        "vec_available": db.VEC_AVAILABLE,
        "current_wiki": wiki,
        "current_wiki_name": wikis.name_of(wiki),
        "wikis": wikis.list_wikis(),
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


def _claude_config(request: Request) -> dict:
    """Build the exact Claude Desktop MCP config for THIS install, with real
    paths auto-filled — so the Help page is copy-paste ready."""
    web_url = f"{request.url.scheme}://{request.url.netloc}"
    data_dir = str(config.DATA_DIR)
    if getattr(sys, "frozen", False):
        # Packaged .app: Claude Desktop launches the binary itself in MCP mode.
        server = {
            "command": sys.executable,
            "env": {"WAIKIKI_MCP": "1", "WAIKIKI_DATA": data_dir,
                    "WAIKIKI_WEB_URL": web_url},
        }
    else:
        # Running from source: launch the venv Python with the package.
        server = {
            "command": sys.executable,
            "args": ["-m", "waikiki.mcp_server"],
            "env": {"PYTHONPATH": str(config.ROOT), "WAIKIKI_DATA": data_dir,
                    "WAIKIKI_WEB_URL": web_url},
        }
    return {"mcpServers": {"waikiki": server}}


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", _ctx(
        request,
        claude_config=json.dumps(_claude_config(request), indent=2),
        config_path=str(Path.home() / "Library" / "Application Support" /
                        "Claude" / "claude_desktop_config.json"),
        web_url=f"{request.url.scheme}://{request.url.netloc}",
    ))


@app.get("/wikis", response_class=HTMLResponse)
def wikis_manage(request: Request, error: str = ""):
    stats = {w["slug"]: wikis.stats(w["slug"]) for w in wikis.list_wikis()}
    return templates.TemplateResponse(request,
        "wikis.html", _ctx(request, error=error, stats=stats))


@app.post("/switch-wiki")
def switch_wiki(wiki: str = Form(...)):
    resp = RedirectResponse("/", status_code=303)
    if wikis.exists(wiki):
        resp.set_cookie("waikiki_wiki", wiki, max_age=60 * 60 * 24 * 365,
                        samesite="lax")
    return resp


@app.post("/wikis/create")
def wikis_create(name: str = Form(...)):
    if not name.strip():
        return RedirectResponse("/wikis?error=Name+is+required", status_code=303)
    slug = wikis.create_wiki(name)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("waikiki_wiki", slug, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/wikis/{slug}/delete")
def wikis_delete(slug: str):
    wikis.delete_wiki(slug)
    return RedirectResponse("/wikis", status_code=303)


@app.get("/wikis/{slug}/export")
def wikis_export(slug: str):
    """Save a wiki to a downloadable .wiki file (browser fallback for Save)."""
    if not wikis.exists(slug):
        raise HTTPException(404, "No such wiki")
    dest = Path(tempfile.mkdtemp()) / f"{slug}.wiki"
    wikis.export_to(slug, str(dest))
    return FileResponse(str(dest), filename=f"{wikis.name_of(slug)}.wiki",
                        media_type="application/octet-stream")


@app.post("/wikis/import")
async def wikis_import(file: UploadFile):
    """Open a wiki from an uploaded file (browser fallback for Open)."""
    tmp = Path(tempfile.mkdtemp()) / (file.filename or "import.wiki")
    tmp.write_bytes(await file.read())
    try:
        slug = wikis.import_from(str(tmp), name=Path(file.filename or "Imported").stem)
    except ValueError as exc:
        return RedirectResponse(f"/wikis?error={exc}", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("waikiki_wiki", slug, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request, template: str = ""):
    markdown = ""
    if template:
        tpl = (store.template_get(int(template)) if template.isdigit()
               else store.template_by_name(template))
        if tpl:
            markdown = tpl["markdown"]
    return templates.TemplateResponse(request,
        "edit.html",
        _ctx(request, page={"slug": "", "title": "", "markdown": markdown}, is_new=True),
    )


@app.get("/templates", response_class=HTMLResponse)
def templates_manage(request: Request):
    return templates.TemplateResponse(request,
        "templates.html", _ctx(request, templates_list=store.templates_list()))


@app.post("/templates/save")
def templates_save(name: str = Form(...), markdown: str = Form(""),
                   tid: str = Form("")):
    if name.strip():
        store.template_save(name.strip(), markdown, int(tid) if tid else None)
    return RedirectResponse("/templates", status_code=303)


@app.post("/templates/{tid}/delete")
def templates_delete(tid: int):
    store.template_delete(tid)
    return RedirectResponse("/templates", status_code=303)


class TableCell(BaseModel):
    table: int
    row: int
    col: int
    value: str


@app.post("/wiki/{slug}/table-cell")
def table_cell_edit(slug: str, body: TableCell):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    try:
        new_md = edits.set_table_cell(page["markdown"], body.table, body.row,
                                      body.col, body.value)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    store.update_page(slug, page["title"], new_md, author="human")
    return {"ok": True}


@app.get("/wiki/{slug}/pdf")
def page_pdf_view(slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    pdf = pdfgen.page_pdf(page["title"], page["html"])
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{slug}.pdf"'})


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
    parent = None
    if page.get("parent_id"):
        prow = db.get_conn().execute(
            "SELECT slug, title FROM pages WHERE id=?", (page["parent_id"],)).fetchone()
        parent = dict(prow) if prow else None
    all_pages = [p for p in store.list_pages() if p["slug"] != slug]
    suggestions = []
    for s in store.suggestions_list(slug):
        full = store.suggestion_get(s["id"])
        s["diff"] = "\n".join(difflib.unified_diff(
            page["markdown"].splitlines(), full["markdown"].splitlines(),
            fromfile="current", tofile="proposed", lineterm=""))
        suggestions.append(s)
    return templates.TemplateResponse(request,
        "page.html", _ctx(request, page=page, versions=store.page_versions(slug),
                          trashed=bool(page.get("deleted_at")),
                          toc=render.extract_toc(page["markdown"]),
                          backlinks=store.backlinks(slug),
                          children=store.children(slug), parent=parent,
                          all_pages=all_pages, tags=store.tags_of(slug),
                          comments=store.comments_list(slug),
                          suggestions=suggestions)
    )


@app.post("/wiki/{slug}/comment")
def add_comment_view(slug: str, body: str = Form(...)):
    store.comment_add(slug, body, author="human")
    return RedirectResponse(f"/wiki/{slug}#comments", status_code=303)


@app.post("/wiki/{slug}/comment/{cid}/resolve")
def resolve_comment_view(slug: str, cid: int):
    store.comment_resolve(cid)
    return RedirectResponse(f"/wiki/{slug}#comments", status_code=303)


@app.post("/wiki/{slug}/suggestion/{sid}/apply")
def apply_suggestion_view(slug: str, sid: int):
    store.suggestion_apply(sid)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.post("/wiki/{slug}/suggestion/{sid}/reject")
def reject_suggestion_view(slug: str, sid: int):
    store.suggestion_reject(sid)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.get("/wikis/{slug}/export-md")
def wikis_export_markdown(slug: str):
    if not wikis.exists(slug):
        raise HTTPException(404, "No such wiki")
    data = wikis.markdown_zip(slug)
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{slug}-markdown.zip"'})


@app.get("/tags", response_class=HTMLResponse)
def tags_view(request: Request):
    return templates.TemplateResponse(request,
        "tags.html", _ctx(request, tags=store.all_tags()))


@app.get("/tag/{tag}", response_class=HTMLResponse)
def tag_index_view(request: Request, tag: str):
    return templates.TemplateResponse(request,
        "tag.html", _ctx(request, tag=tag, pages=store.pages_with_tag(tag)))


@app.get("/changes", response_class=HTMLResponse)
def changes_view(request: Request):
    return templates.TemplateResponse(request,
        "changes.html", _ctx(request, changes=store.recent_changes(limit=100)))


@app.get("/broken-links", response_class=HTMLResponse)
def broken_links_view(request: Request):
    return templates.TemplateResponse(request,
        "broken.html", _ctx(request, broken=store.broken_links()))


@app.get("/wiki/{slug}/edit", response_class=HTMLResponse)
async def edit_page(request: Request, slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    # Seed the CRDT room from the DB before the browser's websocket connects,
    # so the live document already has the page content.
    await collab.ensure_room(db.active_wiki(), slug)
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
    store.soft_delete(slug)   # to the trash (restorable)
    return RedirectResponse("/", status_code=303)


@app.post("/wiki/{slug}/star")
def star_page_view(slug: str, next: str = Form("/")):
    store.toggle_star(slug)
    return RedirectResponse(next or "/", status_code=303)


@app.post("/wiki/{slug}/clone")
def clone_page_view(slug: str):
    page = store.clone_page(slug)
    return RedirectResponse(f"/wiki/{page['slug']}" if page else "/", status_code=303)


@app.post("/wiki/{slug}/parent")
def set_parent_view(slug: str, parent: str = Form("")):
    store.set_parent(slug, parent or None)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.post("/wiki/{slug}/restore")
def restore_page_view(slug: str):
    store.restore(slug)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.post("/wiki/{slug}/purge")
def purge_page_view(slug: str):
    store.hard_delete(slug)   # permanent
    return RedirectResponse("/trash", status_code=303)


@app.get("/trash", response_class=HTMLResponse)
def trash_view(request: Request):
    store.sweep_trash()  # opportunistically enforce retention
    days = db.get_setting("retention_trash_days", "30")
    return templates.TemplateResponse(request,
        "trash.html", _ctx(request, trash=store.list_trash(), retention_days=days))


@app.get("/wiki/{slug}/history/{version_id}", response_class=HTMLResponse)
def history_view(request: Request, slug: str, version_id: int):
    page = store.get_page(slug)
    version = store.get_version(version_id)
    if not page or not version or version["page_id"] != page["id"]:
        raise HTTPException(404, "Version not found")
    diff = "\n".join(difflib.unified_diff(
        version["markdown"].splitlines(), page["markdown"].splitlines(),
        fromfile=f"version @ {version['created_at']}", tofile="current", lineterm=""))
    return templates.TemplateResponse(request, "history.html", _ctx(
        request, page=page, version=version,
        version_html=render.render_markdown(version["markdown"]), diff=diff))


@app.post("/wiki/{slug}/history/{version_id}/restore")
def history_restore(slug: str, version_id: int):
    store.restore_version(slug, version_id)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_view(request: Request, q: str = ""):
    results = rag.search_pages(q) if q else []
    return templates.TemplateResponse(request,
        "search.html", _ctx(request, q=q, results=results)
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, msg: str = "", error: str = ""):
    themes = sorted(p.stem for p in (_STATIC / "themes").glob("*.css"))
    provider, model = embeddings.active()
    return templates.TemplateResponse(request,
        "settings.html",
        _ctx(request, settings=db.all_settings(), themes=themes,
             library=embeddings.get_library(), active_provider=provider,
             active_model=model, catalog=embeddings.fastembed_catalog(),
             msg=msg, error=error),
    )


@app.post("/settings")
def settings_save(theme: str = Form(...),
                  retention_versions: str = Form("50"),
                  retention_trash_days: str = Form("30"),
                  allow_html: str = Form("")):
    db.set_setting("theme", theme)
    db.set_setting("retention_versions", str(max(0, int(retention_versions or 0))))
    db.set_setting("retention_trash_days", str(max(0, int(retention_trash_days or 0))))
    new_html = "1" if allow_html else "0"
    if new_html != db.get_setting("allow_html", "0"):
        db.set_setting("allow_html", new_html)
        store.rerender_all()  # re-render every page under the new HTML policy
    return RedirectResponse("/settings?msg=Saved", status_code=303)


@app.post("/settings/models/add")
def settings_add_model(provider: str = Form(...), slug: str = Form(...)):
    """Add an embedding model by slug, make it active, and re-embed all pages."""
    result = embeddings.add_model(provider, slug)
    if not result["ok"]:
        return RedirectResponse(f"/settings?error={result['error']}", status_code=303)
    rag.reindex_all()
    return RedirectResponse(
        f"/settings?msg=Added+and+activated+{slug}+(dim+{result['dim']})",
        status_code=303,
    )


@app.post("/settings/models/activate")
def settings_activate_model(provider: str = Form(...), model: str = Form(...)):
    embeddings.set_active(provider, model)
    rag.reindex_all()
    return RedirectResponse(f"/settings?msg=Activated+{model}", status_code=303)


@app.get("/image/{image_id}")
@app.get("/image/{image_id}/{filename}")
def serve_image(image_id: int, filename: str = ""):
    # `filename` is only for the URL to carry an extension (media detection); the
    # blob is looked up by id.
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
    if not store.soft_delete(slug):
        raise HTTPException(404, "Page not found or already trashed")
    return {"trashed": slug}


@app.get("/api/search")
def api_search(q: str, k: int = config.RAG_TOP_K):
    """Hybrid BM25 + vector retrieval over the wiki (the RAG endpoint)."""
    return {"query": q, "results": rag.search_chunks(q, k)}


@app.post("/api/images")
async def api_upload_image(file: UploadFile):
    data = await file.read()
    name = file.filename or "image"
    image_id = store.save_image(name, file.content_type or "application/octet-stream", data)
    url = f"/image/{image_id}/{name}"   # filename gives the URL an extension
    return {"id": image_id, "url": url, "markdown": f"![{name}]({url})"}


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


class CollabEdit(BaseModel):
    old: str
    new: str


@app.post("/api/collab/{slug}/edit")
async def api_collab_edit(slug: str, body: CollabEdit):
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    result = await collab.apply_edit(
        wiki, slug, lambda s: edits.plan_edit(s, body.old, body.new))
    return {"wiki": wiki, "slug": slug, **result}


@app.post("/api/collab/{slug}/op")
async def api_collab_op(slug: str, body: dict):
    """Apply a structured text op (edit/remove/prepend/insert/replace_section)
    as a live surgical edit. Used by the MCP text tools."""
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    planner = edits.make_planner(body)
    if planner is None:
        return {"wiki": wiki, "slug": slug, "ok": False,
                "error": f"unknown op '{body.get('op')}'"}
    try:
        result = await collab.apply_edit(wiki, slug, planner)
    except KeyError as exc:
        return {"wiki": wiki, "slug": slug, "ok": False,
                "error": f"missing argument {exc}"}
    return {"wiki": wiki, "slug": slug, **result}


@app.post("/api/collab/{slug}/append")
async def api_collab_append(slug: str, body: CollabAppend):
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    text = await collab.append_text(wiki, slug, body.text)
    return {"wiki": wiki, "slug": slug, "length": len(text)}


@app.post("/api/collab/{slug}/replace")
async def api_collab_replace(slug: str, body: CollabReplace):
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    text = await collab.replace_text(wiki, slug, body.markdown)
    return {"wiki": wiki, "slug": slug, "length": len(text)}


@app.get("/api/collab/{slug}/live")
async def api_collab_live(slug: str):
    """Current live (possibly unsaved) markdown for a page."""
    wiki = db.active_wiki()
    md = await collab.live_markdown(wiki, slug)
    if md is None:
        page = store.get_page(slug)
        if not page:
            raise HTTPException(404, "Page not found")
        md = page["markdown"]
    return {"wiki": wiki, "slug": slug, "markdown": md}
