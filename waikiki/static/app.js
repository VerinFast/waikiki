// Waikiki editor (ES module): EasyMDE + live CRDT collaboration (Yjs) +
// streaming AI (pull) + image upload.
const CFG = window.WAIKIKI || {};
const textarea = document.getElementById("editor");
if (textarea) init();

function init() {
  const EasyMDE = window.EasyMDE;
  const easymde = new EasyMDE({
    element: textarea,
    spellChecker: false,
    autoDownloadFontAwesome: true,
    toolbar: ["bold", "italic", "heading", "|", "quote", "code", "table",
              "unordered-list", "ordered-list", "|", "link", "image",
              {name: "attach", className: "fa fa-paperclip",
               title: "Attach image / video / audio", action: attachMedia},
              {name: "genimage", className: "fa fa-magic",
               title: "Generate an image with AI", action: generateImage},
              "|", "preview", "side-by-side", "fullscreen", "|", "guide"],
    uploadImage: true,
    imageUploadFunction: uploadImage,
  });
  const cm = easymde.codemirror;

  // --- Attach any local media (image / video / audio) via a file picker ---
  function attachMedia() {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/*,video/*,audio/*";
    inp.onchange = () => {
      const f = inp.files[0];
      if (f) uploadImage(f, (url) => insertAtCursor(`![${f.name}](${url})\n`));
    };
    inp.click();
  }

  // --- Generate an image with AI (Claude writes the prompt → agy/gemini renders) ---
  function generateImage() {
    if (!CFG.slug) {
      alert("Save the page first — generated images are stored per article.");
      return;
    }
    const desc = window.prompt("Describe the image to generate:");
    if (!desc || !desc.trim()) return;
    const doc = cm.getDoc();
    const placeholder = `![⏳ generating image…](gen-${Date.now()})`;
    doc.replaceRange("\n" + placeholder + "\n", doc.getCursor());
    const replacePlaceholder = (text) => {
      const idx = easymde.value().indexOf(placeholder);
      if (idx < 0) return;                       // user removed it — leave content alone
      doc.replaceRange(text, doc.posFromIndex(idx),
                       doc.posFromIndex(idx + placeholder.length));
    };
    fetch(`/wiki/${CFG.slug}/generate-image`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: desc.trim() }),
    }).then((r) => r.json()).then((res) => {
      replacePlaceholder(res.ok ? res.markdown : "");
      if (!res.ok) alert("Image generation failed: " + (res.error || "unknown"));
    }).catch((err) => {
      replacePlaceholder("");
      alert("Image generation failed: " + err);
    });
  }

  // Keep the hidden textarea in sync on submit (Save posts current content).
  const form = document.getElementById("editform");
  if (form) form.addEventListener("submit", () => { textarea.value = easymde.value(); });

  // --- Image upload (button / paste / drag) ---
  async function uploadImage(file, onSuccess) {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/images", { method: "POST", body: fd });
    const data = await res.json();
    if (onSuccess) onSuccess(data.url);
    else insertAtCursor(data.markdown + "\n");
    return data.url;
  }
  cm.on("paste", (_cm, ev) => {
    for (const it of (ev.clipboardData || {}).items || []) {
      if (it.type && it.type.startsWith("image/")) {
        ev.preventDefault();
        uploadImage(it.getAsFile(), (url) => insertAtCursor(`![pasted](${url})\n`));
      }
    }
  });
  function insertAtCursor(text) {
    cm.getDoc().replaceRange(text, cm.getDoc().getCursor());
  }

  // --- Live collaboration (CRDT) ---
  if (CFG.collab && CFG.slug) setupCollab(cm);

  // --- Pull-model AI streaming (the "Generate" convenience button) ---
  setupAI(easymde);
  setupMetaTab(easymde);
  setupAutosave(easymde, form);
}

// --- Autosave -----------------------------------------------------------------
// Existing pages are persisted server-side by the CRDT flusher; the user just
// couldn't tell. New pages had no room yet, so their text lived only in the
// browser and closing the tab lost it. Both now save without a click, and say so.
function setupAutosave(easymde, form) {
  const status = document.getElementById("save-status");
  const slugField = form && form.querySelector('input[name="slug"]');
  const titleField = form && form.querySelector('input[name="title"]');
  if (!status || !form) return;

  let dirty = false, saving = false, timer = null, lastSaved = null;

  function show(text, cls) {
    status.textContent = text;
    status.className = "savestatus" + (cls ? " " + cls : "");
  }
  function ago() {
    if (!lastSaved) return "";
    const s = Math.round((Date.now() - lastSaved) / 1000);
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    return Math.round(s / 60) + "m ago";
  }
  function idle() {
    if (dirty) show("Unsaved changes…", "pending");
    else if (lastSaved) show("Saved " + ago(), "ok");
    else show(CFG.collab ? "Saved automatically" : "", "ok");
  }
  setInterval(idle, 5000);

  if (CFG.collab) {
    // Server-side flusher owns persistence here — just reassure the human.
    lastSaved = Date.now();
    show("Saved automatically", "ok");
    easymde.codemirror.on("change", () => { lastSaved = Date.now(); idle(); });
    return;
  }

  async function save() {
    const title = (titleField.value || "").trim();
    if (!title) { show("Add a title to start autosaving", "pending"); return; }
    const markdown = easymde.value();
    if (!markdown.trim() && !slugField.value) return;   // nothing worth creating
    saving = true;
    show("Saving…", "pending");
    try {
      // Pin the wiki explicitly — otherwise this resolves via the cookie and a
      // page opened with ?wiki=X could be autosaved into a different wiki.
      const url = "/api/autosave" + (CFG.wiki ? "?wiki=" + encodeURIComponent(CFG.wiki) : "");
      const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: slugField.value, title, markdown }),
      }).then((r) => r.json());
      if (res.ok) {
        slugField.value = res.slug;      // first save creates; later ones update
        dirty = false; lastSaved = Date.now();
        show("Saved " + ago(), "ok");
      } else {
        show(res.error || "Not saved", "err");
      }
    } catch (e) {
      show("Offline — not saved", "err");
    } finally {
      saving = false;
    }
  }

  function touch() {
    dirty = true;
    show("Unsaved changes…", "pending");
    clearTimeout(timer);
    timer = setTimeout(save, 1500);      // debounce: save shortly after you pause
  }
  easymde.codemirror.on("change", touch);
  if (titleField) titleField.addEventListener("input", touch);

  // A manual Save is a normal form post; don't warn about it.
  form.addEventListener("submit", () => { dirty = false; });
  window.addEventListener("beforeunload", (e) => {
    if (!dirty && !saving) return;
    e.preventDefault();
    e.returnValue = "";                  // browser shows its own confirm dialog
  });
}

// --- Metadata tab: edit frontmatter properties as fields ---
const _FM_RE = /^\s*---[ \t]*\n([\s\S]*?)\n---[ \t]*\n?/;

function setupMetaTab(easymde) {
  const panel = document.getElementById("meta-panel");
  const tabs = document.querySelectorAll(".edit-tabs .tab");
  if (!panel || !tabs.length) return;
  const container = document.querySelector(".EasyMDEContainer");

  tabs.forEach((t) => t.addEventListener("click", () => {
    tabs.forEach((x) => x.classList.remove("on"));
    t.classList.add("on");
    const isMeta = t.getAttribute("data-tab") === "meta";
    if (container) container.style.display = isMeta ? "none" : "";
    panel.hidden = !isMeta;
    if (isMeta) loadRows();
  }));

  function loadRows() {
    const m = easymde.value().match(_FM_RE);
    const rows = [];
    if (m) m[1].split("\n").forEach((line) => {
      const i = line.indexOf(":");
      if (i > 0) rows.push([line.slice(0, i).trim(), line.slice(i + 1).trim()]);
    });
    const tbody = document.getElementById("meta-rows");
    tbody.innerHTML = "";
    (rows.length ? rows : [["", ""]]).forEach((kv) => addRow(kv[0], kv[1]));
  }

  function addRow(k, v) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td><input class="mk" placeholder="key"></td>'
      + '<td><input class="mv" placeholder="value"></td>'
      + '<td><button type="button" class="btn small mdel">✕</button></td>';
    tr.querySelector(".mk").value = k;
    tr.querySelector(".mv").value = v;
    tr.querySelector(".mdel").addEventListener("click", () => tr.remove());
    document.getElementById("meta-rows").appendChild(tr);
  }

  document.getElementById("meta-add").addEventListener("click", () => addRow("", ""));

  document.getElementById("meta-apply").addEventListener("click", () => {
    const lines = [];
    document.querySelectorAll("#meta-rows tr").forEach((tr) => {
      const k = tr.querySelector(".mk").value.trim();
      const v = tr.querySelector(".mv").value.trim();
      if (k) lines.push(k + ": " + v);
    });
    const fm = lines.length ? "---\n" + lines.join("\n") + "\n---\n" : "";
    const doc = easymde.codemirror.getDoc();
    const m = easymde.value().match(_FM_RE);
    if (m) doc.replaceRange(fm, { line: 0, ch: 0 }, doc.posFromIndex(m[0].length));
    else doc.replaceRange(fm, { line: 0, ch: 0 });
    document.querySelector('.edit-tabs .tab[data-tab="content"]').click();
  });
}

async function setupCollab(cm) {
  const status = document.getElementById("presence");
  try {
    const Y = await import("yjs");
    const { WebsocketProvider } = await import("y-websocket");
    const { CodemirrorBinding } = await import("y-codemirror");

    const ydoc = new Y.Doc();
    const ytext = ydoc.getText("content");
    const provider = new WebsocketProvider(CFG.wsBase, CFG.slug, ydoc);
    // Binds the shared text + remote cursors into the editor.
    new CodemirrorBinding(ytext, cm, provider.awareness);

    const me = { name: randomName(), color: randomColor() };
    provider.awareness.setLocalStateField("user", me);

    const render = () => renderPresence(provider.awareness, status);
    provider.awareness.on("change", render);
    provider.on("status", (e) => {
      if (status && e.status !== "connected") status.dataset.conn = e.status;
      render();
    });
    render();
  } catch (err) {
    console.error("collab init failed", err);
    if (status) status.textContent = "(offline — live sync unavailable)";
  }
}

function renderPresence(awareness, el) {
  if (!el) return;
  const users = [];
  awareness.getStates().forEach((state) => {
    if (state.user) users.push(state.user);
  });
  el.innerHTML = users
    .map((u) => `<span class="peer" style="--c:${u.color}">${escapeHtml(u.name)}</span>`)
    .join("");
}

function setupAI(easymde) {
  const aiGo = document.getElementById("ai-go");
  const aiPrompt = document.getElementById("ai-prompt");
  const aiRag = document.getElementById("ai-rag");
  const aiStatus = document.getElementById("ai-status");
  if (!aiGo) return;

  const run = async () => {
    const prompt = aiPrompt.value.trim();
    if (!prompt) return;
    aiGo.disabled = true;
    aiStatus.textContent = "thinking…";
    const cm = easymde.codemirror;
    const ins = (t) => cm.getDoc().replaceRange(t, cm.getDoc().getCursor());
    ins("\n");
    try {
      const res = await fetch("/api/ai/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, page_context: easymde.value(), use_rag: aiRag.checked }),
      });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      aiStatus.textContent = "writing…";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop();
        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const p = JSON.parse(line.slice(5).trim());
          if (p.text) ins(p.text);
          else if (p.error) aiStatus.textContent = "error: " + p.error;
          else if (p.done) aiStatus.textContent = "done";
        }
      }
    } catch (e) {
      aiStatus.textContent = "error: " + e.message;
    } finally {
      aiGo.disabled = false;
      aiPrompt.value = "";
    }
  };
  aiGo.addEventListener("click", run);
  aiPrompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); run(); }
  });
}

// --- helpers ---
const NAMES = ["Otter", "Heron", "Marlin", "Coral", "Reef", "Kai", "Nalu", "Moana"];
function randomName() { return NAMES[Math.floor(Math.random() * NAMES.length)]; }
function randomColor() {
  const h = Math.floor(Math.random() * 360);
  return `hsl(${h} 70% 45%)`;
}
function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
