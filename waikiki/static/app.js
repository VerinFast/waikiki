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
