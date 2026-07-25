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

## Sprint B — Authoring & media  ✅ shipped (v0.0.5)
- [x] Arbitrary HTML snippets (per-wiki toggle in Settings)
- [x] Templates (built-ins + editable; New-from-template; MCP `create_from_template`)
- [x] Image / video / audio embedding (mp4/mp3/… render as players) + MCP
      `upload_asset` (path or base64)
- [x] Print to PDF (page → PDF via xhtml2pdf: `/wiki/{slug}/pdf`, Print button,
      MCP `export_pdf`)
- [x] Zip save bundle (`.wiki` is now a zip of db + media/ + manifest; import
      auto-detects zip vs raw db)
- [x] External links — open-in-new-tab + ↗ marker
- [x] Links to local files (`file://` allowed; `javascript:` blocked)
- [ ] Table cell editing — deferred (a WYSIWYG table widget; doesn't fit the
      markdown editor well — revisit)

## Sprint C — Structure & reuse  ✅ shipped (v0.0.6)
- [x] Structured data — YAML-lite frontmatter properties render as an **infobox**
- [x] Transclusion / includes — `![[Page]]` / `![[Page#Section]]` embeds
- [x] Tags / frontmatter + auto-index pages (`/tags`, `/tag/{tag}`; MCP
      `list_tags` / `pages_by_tag`)
- [x] Components — ` ```timeline ` fenced block renders a timeline (extensible)
- [ ] Follow-up: query pages by arbitrary property value (heatmap/grid views) —
      infobox + tag-index cover display + one query axis for now

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
