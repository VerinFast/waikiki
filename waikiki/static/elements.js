// Waikiki custom-element runtime. Registers each element definition emitted by
// the server (as <script class="wk-element-defs">) as an HTML5 Web Component with
// a Shadow DOM — scoped CSS + encapsulated JS. Field values are interpolated into
// the template ({{field}}) with HTML escaping; richer behavior can use the js.
(function () {
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  // {{field}} uses the server-rendered value, so [[wiki links]] inside a field
  // become real links without the element having to parse them itself. The
  // server escapes these before adding anchors, so they're safe to inject.
  function interp(tpl, props, rich) {
    return (tpl || "").replace(/\{\{\s*([\w.\- ]+?)\s*\}\}/g, function (_, k) {
      var key = k.trim();
      if (rich && rich[key] != null) return rich[key];
      var v = props[key];
      return v == null ? "" : esc(String(v));
    });
  }
  function register(def) {
    var tag = def && def.tag;
    if (!tag || customElements.get(tag)) return;
    customElements.define(tag, class extends HTMLElement {
      connectedCallback() {
        if (this._wkDone) return;
        this._wkDone = true;
        var props = {}, rich = {};
        try { props = JSON.parse(this.getAttribute("data-props") || "{}"); } catch (e) {}
        try { rich = JSON.parse(this.getAttribute("data-html") || "{}"); } catch (e) {}
        var root = this.attachShadow({ mode: "open" });
        root.innerHTML = "<style>" + (def.css || "") + "</style>"
                       + interp(def.html || "", props, rich);
        if (def.js) {
          // `html` holds each field already rendered (wiki links resolved);
          // assign it with innerHTML where you want links, props for plain text.
          try { new Function("root", "props", "host", "html", def.js)(root, props, this, rich); }
          catch (e) { console.error("[waikiki element] " + tag, e); }
        }
      }
    });
  }
  function scan() {
    document.querySelectorAll("script.wk-element-defs").forEach(function (s) {
      if (s._wkScanned) return;
      s._wkScanned = true;
      var defs;
      try { defs = JSON.parse(s.textContent); } catch (e) { return; }
      (defs || []).forEach(register);
    });
  }
  if (document.readyState !== "loading") scan();
  else document.addEventListener("DOMContentLoaded", scan);
  window.wkScanElements = scan;   // let the editor preview re-scan
})();
