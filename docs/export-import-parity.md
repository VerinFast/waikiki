# Every export has an import

*Architectural rule 12. This is a standing decision, not a description of the
current code — if the two ever disagree, the code is wrong.*

## The rule

**If Waikiki can write wiki state to a file, Waikiki must be able to read that
file back.** An export you cannot restore is not a backup; it is a file that
looks like one, and the person finds out on the day they need it. Shipping an
export surface without its reader is incomplete work, not a follow-up ticket.

The rule is about the **pair being reachable**, which is stronger than both
halves existing. That distinction is the whole reason this document exists.

## Why it is written down

`store.export_wiki_bundle` and `store.import_wiki_bundle` shipped together. Both
were correct. `tests/test_wiki_bundle.py` round-tripped a populated wiki through
a bundle and back, in a file, and passed.

A person still could not open a bundle. **Manage wikis → Open** is the only door
they can reach, and it went to `wikis.import_from`, which knew exactly one
archive shape: the zip that wraps a `wiki.db`. Handed a bundle, it looked for a
`.db` member, found none, and said:

```
Zip bundle has no wiki.db
```

So Kahala could export a wiki that Waikiki could read, and did not. The importer
existed, was tested, and was unreachable — and the error blamed the file. Nothing
in the codebase was positioned to notice, because every test asked "does the
round-trip work?" and none asked "can anyone get to it?"

## What it requires in practice

**One door, and it sniffs.** `wikis.import_from` is the single entry point for a
file a person picked, and it decides by format tag, never by name:

| Shape | Carries | Detected by |
|---|---|---|
| Wiki file | a whole SQLite database (`wiki.db`), or a raw `.db` | a `.db` member, or the SQLite header |
| Interchange bundle | `manifest.json` + `pages/<slug>.snapshot` per page; **no database** | `manifest.json`'s `format` tag |
| Markdown | one `<slug>.md` per page and nothing structural | `.md` members, with no database and no manifest |

All three are zips (markdown also arrives as a plain directory). A file-picker
filter is not a format check and an extension is not evidence —
`tests/test_wiki_bundle.py` opens a bundle named `.wiki` and a wiki file named
`.zip`, in both directions, and `tests/test_markdown_roundtrip.py` opens a
markdown zip named `.wiki`, for exactly that reason. The order is part of the
decision: a `.wiki` save that happens to carry a stray `.md` is still a wiki
file, so the database and the manifest are both ruled out before the markdown
sniff is asked.

**Sniffing is also what keeps the refusal honest.** A bundle the version gate
rejects has to say *that*. The alternative is the original bug wearing a
different hat: the wiki-file branch reporting "not a Waikiki wiki file" about a
perfectly valid bundle, sending the reader off to check the wrong thing.

**A bundle lands through the repository.** `_import_bundle` registers an empty
wiki and calls `store.import_wiki_bundle` — the same call Kahala's pull makes
(rule 7) — so pages are rendered, versioned and re-embedded locally as they
arrive. It deliberately does *not* end in `_reindex_import`: the embeddings were
regenerated page by page on the way in, so there is no imported index to
distrust.

**Failure splits the way a pull's does, and for the same reason.** "Refused" is a
promise that nothing changed, so it is only said when that is true:

- **Refused** — every gate runs before the first write, so the empty wiki that
  was just registered is removed again rather than stranding a shell in the
  registry.
- **Partly imported** — a failure *after* writes began keeps the wiki and names
  it as partial. It is one Waikiki created this call, never a file of the user's,
  but a half-imported wiki reported as success is worse than either honest
  answer.

## The exemption, and the one format that had to be decided

**PDF is exempt, permanently.** `mcp_server.export_pdf` and the route above it
render a page for a person to read. A PDF is a lossy presentation, not a
serialization of wiki state — there is nothing to restore from, and there never
will be. The test enforces the shape of this reasoning: an exemption must claim
to be a render target.

**Markdown was the one open violation, and is now closed.**
`wikis.export_markdown` — with `/wikis/{slug}/export-md` and the MCP tool over
it — wrote every page to a directory as `<slug>.md`, and nothing read a
directory of them back. It sat on the known-gap list rather than being quietly
tolerated, because closing it meant deciding what a folder of markdown *means*
on the way in. Those decisions are below; `wikis.import_markdown` is the reader,
and `tests/test_markdown_roundtrip.py` is the proof.

### What a folder means on the way in

**A new wiki by default, a merge when the caller names the target.** Open — the
door a person reaches — has never modified a wiki that was already here, and a
folder of markdown carries no label saying which wiki it came from the way a
bundle does. So it lands as a new one. `into=<slug>` merges instead, which is
the round-trip the MCP tool exists for: export to a repo's `docs/`, edit the
text there, bring it back.

**A merge is by slug, and nothing is deleted.** A page whose slug is already
there is updated in place, which means *versioned* — the text it had is one
click away in its history, because an import that silently replaced a page the
person had since edited would be a data-loss path wearing a restore's clothes.
A page that is in the wiki and not in the folder is left alone: markdown cannot
express a deletion, so absent means absent, not deleted.

### Frontmatter: what is honoured, what is ignored

`title` and `parent` are **structure**; everything else is the page's own.

The exported file carries a frontmatter header, which it did not before. The
body alone does not name the page: the slug is in the filename, but the title is
a column, and Waikiki resolves `[[links]]` by *title* — so a wiki restored from
filenames alone comes back with every link pointing at a page that no longer
answers to that name. `title:` costs one line and fixes it.

On the way in, `title:` and `parent:` are read into the page's columns and taken
back *out* of the text (`structure.take_structural`). Left in, they would render
as infobox rows restating what the page already is, and the next export would
emit them twice. `tags:` and every other key are stored verbatim, so the
ordinary write path indexes them exactly as if a person had typed them — the
importer never re-derives what the page's own text already says. A file with no
header at all still imports: the title falls back to the first `# heading`, then
to the filename. Export → import → export is a fixed point, which is the
property that says the two halves agree about the format.

The one thing this drops silently: a page whose *own* property is named `title`
or `parent`. There is one `title:` line and the page's actual title has to be
it.

### Slugs and collisions

**The filename is the slug**, because it was the slug on the way out —
anything else and nothing that references the page resolves. A nested file is
taken by its basename: a subfolder is not hierarchy (`parent:` is), so
`guides/setup.md` is the page `setup`.

An existing page with that slug **merges** (above). Two files in one import
claiming the same slug is **refused before the first write**, naming both, since
one page cannot be two files and picking a winner would discard the other
without saying so. Failure splits the way a bundle's does and for the same
reason — "refused" is a promise that nothing changed, so a refusal removes the
empty wiki that had just been registered, and only a failure *after* writes
began is reported as partly imported.

### Hierarchy: recorded, not accepted as lost

Markdown export was flat, and the choice was to accept that or to start
recording it. It records it: `parent:` on a sub-page's file costs one line and
is the difference between a 215-page wiki coming back as 215 orphans and coming
back as itself. Placement runs after every page has landed, so a parent further
down the folder still resolves; a `parent:` naming a page that is in neither the
folder nor the wiki leaves its child at top level and is *reported*
(`unplaced`), because one stale line in a hand-edited folder should not fail an
import.

A file with **no** `parent:` never moves an existing page to top level. Markdown
cannot tell "top level" apart from "doesn't say", and a merge must not
restructure a wiki the folder never claimed to describe.

### What markdown still does not carry

Text only, one file per page: **no images, history, comments, suggestions,
templates, elements, trash, manual order or starred flags.** That is not a gap
under this rule — the rule is that what the export *writes* can be read back,
and it can. But it is why markdown is the shape for a repo and `.wiki` or a
bundle is the faithful copy, and the UI should never imply otherwise.

### Reachable, not merely present

The whole reason this document exists is that a reader can exist and not be
reachable, so the markdown one arrives at every door: `wikis.import_from` sniffs
a markdown archive (a zip carrying `.md` and neither a database nor an
interchange manifest) and a directory, so the **Export .md** download re-opens
through Open and the browser upload route; the desktop app adds *Open markdown
folder…*, since a file dialog cannot pick the directory `export_markdown`
writes; and `mcp_server.import_markdown` is the agent's half. Order matters in
the sniffing, as it does for bundles: the wiki-file branch says "no wiki.db"
about anything it cannot parse, which is the wrong thing to tell someone holding
a perfectly good export of another shape.

## The guard

`tests/test_export_import_parity.py` parses the package for every `export*`
function and fails on one that has neither a named importer, a recorded
exemption, nor a place on the known-gap list — which is now **empty**, markdown
having been the last entry on it. Parsed rather than imported, so discovery does
not depend on a module's import side effects.

It checks the surface, not the round-trips — those are proved in
`test_wiki_bundle.py`, `test_ydoc_interchange.py`, `test_markdown_roundtrip.py`
and `test_wikis.py`. What this guard buys is that a **new** export cannot arrive
without its reader, and that the gap list cannot grow without saying so in a
diff someone reviews.
