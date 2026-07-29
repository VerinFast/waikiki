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
  function interp(tpl, props) {
    return (tpl || "").replace(/\{\{\s*([\w.\- ]+?)\s*\}\}/g, function (_, k) {
      var v = props[k.trim()];
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
        var props = {};
        try { props = JSON.parse(this.getAttribute("data-props") || "{}"); } catch (e) {}
        var root = this.attachShadow({ mode: "open" });
        root.innerHTML = "<style>" + (def.css || "") + "</style>" + interp(def.html || "", props);
        if (def.js) {
          try { new Function("root", "props", "host", def.js)(root, props, this); }
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
