# Waikiki roadmap

Backlog from the human (author) and Claude (the MCP co-editor), grouped into
sprints. Shipped items move to CHANGELOG via git history.

## Sprint A — Co-editing core  ✅ shipped (v0.0.3)
- [x] Section/text edit tools: `replace_section`, `insert_after`/`insert_before`
      (by heading, anchor text, or char position), `prepend`, `remove`
- [x] `get_page` returns the heading outline/anchors
- [x] Change feed: `changes_since(T)` MCP tool + Recent Changes page
- [x] Backlinks ("what links here") + broken-link report
- [x] Link-by-title (`[[Title]]` resolves to the current page; links survive renames)

Already done in earlier work: per-page history + diffs + revert; within-page
find/replace (`edit_page`); broken-link counts in wiki stats.

## Sprint B — Authoring & media
- [ ] Arbitrary HTML snippets (per-wiki toggle)
- [ ] Templates (new pages start from content)
- [ ] Image / video / audio embedding + MCP asset upload (path or base64)
- [ ] Print to PDF (single page; UI + MCP)
- [ ] Zip save bundle (db + media + extras) — replaces the single-.wiki export
- [ ] External links — `[text](https://…)` styling + open-in-new-tab (bare-URL
      autolinking already works via linkify)
- [ ] Links to local files (`file://` / asset references) — pairs with media
- [ ] Table cell editing — richer in-editor table UI (edit a single cell)

## Sprint C — Structure & reuse
- [ ] Structured data / property tables (typed fields → queryable grid/heatmap views)
- [ ] Transclusion / includes (one source block embedded in many pages)
- [ ] Tags / frontmatter + auto-generated index pages
- [ ] Components (Wikipedia-style), starting with timelines

## Sprint D — Review & interop
- [ ] Comments / suggestion mode; propose-vs-apply for big rewrites
- [ ] Markdown/git export to `docs/` (round-trip)
- [ ] Universal2 + signed/notarized macOS build

## Sprint E — Polish & structure  ✅ shipped (v0.0.4)
- [x] Clone an article (UI + MCP `clone_page`)
- [x] App icon — 🌺 hibiscus over 💾 floppy (assets/Waikiki.icns)
- [x] Connect Claude help page refreshed with the current toolset
- [x] Parent pages — mark a page a child of another; children are hidden from
      the rail and excluded from the main RAG index; their vectors live in a
      partitioned sub-index (`vec_chunks_sub`, keyed by parent) with
      `set_parent` / `list_children` / `search_subpages` tools
- [x] Bug: Contents/anchor links now scroll reliably (explicit window scroll)
