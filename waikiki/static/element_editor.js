// Custom-element editor: CodeMirror syntax highlighting + Format (js-beautify) +
// a live Shadow-DOM preview. Loaded only on the element edit page. Degrades to
// plain textareas if the CDN can't be reached (offline).
(function () {
  var CM = "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/";
  var BE = "https://cdnjs.cloudflare.com/ajax/libs/js-beautify/1.15.1/";
  var scripts = [
    CM + "codemirror.min.js",
    CM + "mode/xml/xml.min.js",
    CM + "mode/javascript/javascript.min.js",
    CM + "mode/css/css.min.js",
    CM + "mode/htmlmixed/htmlmixed.min.js",
    BE + "beautify.min.js", BE + "beautify-css.min.js", BE + "beautify-html.min.js",
  ];

  function loadCss(href) {
    var l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href; document.head.appendChild(l);
  }
  function loadSeq(list, done) {
    (function next(i) {
      if (i >= list.length) return done();
      var s = document.createElement("script");
      s.src = list[i]; s.onload = function () { next(i + 1); };
      s.onerror = function () { done(new Error("load failed: " + list[i])); };
      document.head.appendChild(s);
    })(0);
  }

  var editors = {};      // id -> CodeMirror instance (or null if plain textarea)
  var previewN = 0;

  function val(id) {
    return editors[id] ? editors[id].getValue()
                       : (document.getElementById(id) || {}).value || "";
  }
  function setVal(id, v) {
    if (editors[id]) editors[id].setValue(v);
    else if (document.getElementById(id)) document.getElementById(id).value = v;
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function interp(tpl, props) {
    return (tpl || "").replace(/\{\{\s*([\w.\- ]+?)\s*\}\}/g, function (_, k) {
      var v = props[k.trim()]; return v == null ? "" : esc(String(v));
    });
  }
  function sampleProps() {
    var props = {};
    (document.getElementById("ed-fields").value || "").split("\n").forEach(function (line) {
      var name = line.split("|")[0].replace("*", "").trim();
      if (name) props[name] = "Sample " + name;
    });
    if (!Object.keys(props).length) props.title = "Sample";
    return props;
  }
  // Exposed so a drafted element can be shown before it is saved (#34).
  window.wkElementPreview = function () { try { preview(); } catch (e) {} };
  function preview() {
    var host = document.getElementById("el-preview");
    var tag = "wk-preview-" + (previewN++);
    var html = val("ed-html"), js = val("ed-js"), props = sampleProps();
    try {
      customElements.define(tag, class extends HTMLElement {
        connectedCallback() {
          var root = this.attachShadow({ mode: "open" });
          root.innerHTML = "<style>" + val("ed-css") + "</style>" + interp(html, props);
          if (js) { try { new Function("root", "props", "host", js)(root, props, this); } catch (e) { root.innerHTML += "<pre style='color:red'>" + esc(e.message) + "</pre>"; } }
        }
      });
    } catch (e) { host.innerHTML = "<span class='muted'>preview error: " + esc(e.message) + "</span>"; return; }
    host.innerHTML = "";
    var el = document.createElement(tag);
    el.setAttribute("data-props", JSON.stringify(props));
    host.appendChild(el);
  }

  var wired = false;
  function wire() {
    if (wired) return;
    wired = true;
    document.querySelectorAll("[data-fmt]").forEach(function (b) {
      b.addEventListener("click", function () {
        var kind = b.getAttribute("data-fmt"), id = "ed-" + kind;
        try {
          if (kind === "html" && window.html_beautify) setVal(id, window.html_beautify(val(id), { indent_size: 2 }));
          else if (kind === "css" && window.css_beautify) setVal(id, window.css_beautify(val(id), { indent_size: 2 }));
          else if (kind === "js" && window.js_beautify) setVal(id, window.js_beautify(val(id), { indent_size: 2 }));
        } catch (e) { /* ignore */ }
      });
    });
    var pb = document.getElementById("el-preview-btn");
    if (pb) pb.addEventListener("click", preview);
  }

  function initEditors() {
    if (!window.CodeMirror) { wire(); return; }   // plain textareas + Format/Preview
    var modes = { "ed-html": "htmlmixed", "ed-css": "css", "ed-js": "javascript" };
    Object.keys(modes).forEach(function (id) {
      var ta = document.getElementById(id);
      if (!ta) return;
      editors[id] = CodeMirror.fromTextArea(ta, {
        mode: modes[id], lineNumbers: true, lineWrapping: true,
        tabSize: 2, indentUnit: 2, viewportMargin: Infinity,
      });
    });
    wire();
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("element-form")) return;
    loadCss(CM + "codemirror.min.css");
    loadSeq(scripts, function () { initEditors(); });
    // If the CDN never responds, still wire Format/Preview after a moment.
    setTimeout(function () { if (!window.CodeMirror && !Object.keys(editors).length) wire(); }, 4000);
  });
})();
