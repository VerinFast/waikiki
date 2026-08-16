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
  // Replace any [[wiki link]] left as literal text inside the shadow root with
  // the anchor the server already rendered for it.
  //
  // An element that writes props into the DOM itself — `el.textContent =
  // props.title` — never sees the resolved values, so its links used to show up
  // as raw [[brackets]] unless the component shipped its own link parser. This
  // sweep runs after the component's JS, so that shim is no longer needed and
  // resolution stays server-side (link-by-title, red links) where the page index
  // lives.
  var LINK_RE = /\[\[[^\]]+\]\]/g;
  function linkifyShadow(root, links) {
    if (!links) return;
    var keys = Object.keys(links);
    if (!keys.length) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        // Never rewrite inside style/script, or text already inside a link.
        for (var p = node.parentNode; p && p !== root; p = p.parentNode) {
          var name = p.nodeName;
          if (name === "STYLE" || name === "SCRIPT" || name === "A") {
            return NodeFilter.FILTER_REJECT;
          }
        }
        return LINK_RE.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT
                                            : NodeFilter.FILTER_REJECT;
      }
    });
    var targets = [], n;
    while ((n = walker.nextNode())) targets.push(n);   // collect before mutating

    targets.forEach(function (node) {
      var frag = document.createDocumentFragment();
      var text = node.nodeValue, last = 0, m;
      LINK_RE.lastIndex = 0;
      while ((m = LINK_RE.exec(text))) {
        var anchor = links[m[0]];
        if (anchor == null) continue;          // not a link we resolved; leave it
        if (m.index > last) {
          frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        }
        var holder = document.createElement("span");
        holder.innerHTML = anchor;             // server-built, already escaped
        while (holder.firstChild) frag.appendChild(holder.firstChild);
        last = m.index + m[0].length;
      }
      if (!last) return;                        // nothing substituted
      if (last < text.length) {
        frag.appendChild(document.createTextNode(text.slice(last)));
      }
      node.parentNode.replaceChild(frag, node);
    });
  }

  function register(def) {
    var tag = def && def.tag;
    if (!tag || customElements.get(tag)) return;
    customElements.define(tag, class extends HTMLElement {
      connectedCallback() {
        if (this._wkDone) return;
        this._wkDone = true;
        var props = {}, rich = {}, links = {};
        try { props = JSON.parse(this.getAttribute("data-props") || "{}"); } catch (e) {}
        try { rich = JSON.parse(this.getAttribute("data-html") || "{}"); } catch (e) {}
        try { links = JSON.parse(this.getAttribute("data-links") || "{}"); } catch (e) {}
        var root = this.attachShadow({ mode: "open" });
        root.innerHTML = "<style>" + (def.css || "") + "</style>"
                       + interp(def.html || "", props, rich);
        if (def.js) {
          // `html` holds each field already rendered (wiki links resolved);
          // assign it with innerHTML where you want links, props for plain text.
          // Either way the sweep below catches any [[link]] left as text.
          try { new Function("root", "props", "host", "html", def.js)(root, props, this, rich); }
          catch (e) { console.error("[waikiki element] " + tag, e); }
        }
        try { linkifyShadow(root, links); }
        catch (e) { console.error("[waikiki element linkify] " + tag, e); }
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
