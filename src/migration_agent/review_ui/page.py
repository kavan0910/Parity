"""The single self-contained review page (no external network)."""
from __future__ import annotations

import re


def render_page(base: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_-]+", base):
        raise ValueError("bad base path")
    return _TEMPLATE.replace("__BASE__", base)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration review</title>
<script>
try { var t = localStorage.getItem('lavish-theme'); document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light'); } catch (e) { document.documentElement.setAttribute('data-theme', 'light'); }
</script>
<style>
:root { --bg:#f7f8fa; --panel:#fff; --text:#1c2430; --muted:#5d6879; --border:#d9dee6; --accent:#2457d6; --accent-text:#fff; --good:#138a43; --warn:#c77700; --bad:#cc2f3a; }
:root[data-theme="dark"] { --bg:#12161d; --panel:#1b212b; --text:#e4e9f1; --muted:#9aa5b8; --border:#2d3644; --accent:#7ba3ff; --accent-text:#10151c; --good:#4cd081; --warn:#f0b04a; --bad:#ff6b75; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; display: flex; flex-direction: column; }
header { display: flex; gap: 10px; align-items: center; padding: 10px 20px; background: var(--panel); border-bottom: 1px solid var(--border); }
header h1 { font-size: 17px; margin: 0; font-weight: 650; flex: 1; }
button, select, input, textarea { background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 7px 12px; font: inherit; }
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
textarea { width: 100%; min-height: 70px; resize: vertical; }
nav.tabs { display: flex; gap: 4px; padding: 0 20px; background: var(--panel); border-bottom: 1px solid var(--border); overflow-x: auto; }
nav.tabs button { border: 0; border-bottom: 3px solid transparent; border-radius: 0; background: none; padding: 11px 14px; font-weight: 600; color: var(--muted); white-space: nowrap; }
nav.tabs button.on { color: var(--text); border-bottom-color: var(--accent); }
.count { background: var(--bad); color: #fff; border-radius: 10px; font-size: 12px; padding: 0 7px; margin-left: 6px; }
main { flex: 1; overflow-y: auto; padding: 24px 20px 80px; }
.wrap { max-width: 820px; margin: 0 auto; }
h2.title { font-size: 24px; margin: 0 0 6px; }
.lead { color: var(--muted); margin: 0 0 18px; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 18px; }
.chips button { border-radius: 16px; padding: 4px 12px; font-size: 13px; }
.chips button.on { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
main code, main pre { background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; font-size: 13px; }
main pre { padding: 8px; overflow-x: auto; }
.iid { color: var(--accent); cursor: pointer; border-bottom: 1px dotted var(--accent); }
.iid.has { background: color-mix(in srgb, var(--warn) 25%, transparent); }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin: 0 0 14px; }
.card h3 { margin: 0 0 4px; font-size: 16px; }
.muted { color: var(--muted); font-size: 13px; }
.pill { display: inline-block; border: 1px solid var(--border); border-radius: 10px; padding: 0 8px; font-size: 12px; margin-right: 4px; }
.sev-high { color: var(--bad); } .sev-medium { color: var(--warn); } .sev-low { color: var(--good); }
blockquote { margin: 8px 0; padding: 2px 10px; border-left: 3px solid var(--border); color: var(--muted); font-size: 13px; }
.row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.empty { text-align: center; padding: 40px 10px; color: var(--muted); }
.empty b { display: block; font-size: 20px; color: var(--text); margin-bottom: 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; margin-bottom: 14px; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; }
.stat b { display: block; font-size: 22px; }
.state-approved { color: var(--good); } .state-stale { color: var(--warn); } .state-none { color: var(--muted); }
ul.blockers li { color: var(--bad); } ul.warnings li { color: var(--warn); }
button:disabled { opacity: .5; cursor: not-allowed; }
.added { color: var(--good); } .removed { color: var(--bad); } .changed { color: var(--warn); }
iframe { width: 100%; height: 72vh; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); }
#drawer { position: fixed; right: 16px; bottom: 16px; width: min(360px, calc(100vw - 32px)); max-height: 70vh; overflow-y: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; box-shadow: 0 8px 30px rgba(0,0,0,.18); display: none; }
#drawer h2 { margin: 0 0 8px; font-size: 15px; }
#drawer .x { float: right; padding: 0 8px; }
#toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); border-radius: 8px; padding: 8px 14px; display: none; z-index: 30; }
#tour { position: fixed; inset: 0; background: rgba(10,14,20,.55); display: none; align-items: center; justify-content: center; z-index: 20; padding: 16px; }
#tour .box { background: var(--panel); border-radius: 16px; padding: 26px; width: min(460px, 100%); box-shadow: 0 12px 50px rgba(0,0,0,.35); }
#tour .big { font-size: 40px; line-height: 1; margin-bottom: 8px; }
#tour h2 { margin: 0 0 8px; font-size: 21px; }
#tour p { margin: 0 0 18px; color: var(--muted); }
#tour .dots { display: flex; gap: 6px; margin-bottom: 16px; }
#tour .dots i { width: 8px; height: 8px; border-radius: 50%; background: var(--border); }
#tour .dots i.on { background: var(--accent); }
#tour .btns { display: flex; gap: 8px; justify-content: space-between; }
</style>
</head>
<body>
<header>
  <h1 id="pname">Migration review</h1>
  <button id="help" type="button">? Help</button>
  <button id="theme" type="button">Theme</button>
</header>
<nav class="tabs" id="tabs" aria-label="Views"></nav>
<main id="main" tabindex="-1"></main>
<aside id="drawer" aria-label="Comments"></aside>
<div id="tour" role="dialog" aria-modal="true" aria-label="Quick tour"></div>
<div id="toast" role="status"></div>
<script>
(function () {
  "use strict";
  var BASE = "__BASE__";
  var S = null, tab = "doc", secId = null, selected = null, showAll = false;
  var STEPS = [
    ["👋", "Welcome", "This tool shows what the AI understood about your old code, and lets you correct it. It takes about a minute to learn."],
    ["📄", "Read the Document", "The Document tab explains your system in plain language. Pick a section with the buttons at the top."],
    ["💬", "Leave a comment", "See a dotted blue ID like a name or code? Click it to write a comment. The AI uses your comments next time."],
    ["✅", "Check the problems", "The To check tab lists places where the document and the code disagree. Click one answer for each. That is all."],
    ["🗺️", "See the Map", "The Map tab draws how the parts connect. History and Numbers are under More. You can reopen this tour anytime with Help."]
  ];

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) { if (k === "text") e.textContent = attrs[k]; else if (k === "cls") e.className = attrs[k]; else e.setAttribute(k, attrs[k]); }
    (kids || []).forEach(function (c) { if (c != null) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return e;
  }
  function toast(msg) { var t = document.getElementById("toast"); t.textContent = msg; t.style.display = "block"; setTimeout(function () { t.style.display = "none"; }, 2500); }
  function api(path, opts) {
    return fetch(BASE + path, opts).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; }); });
  }
  function post(path, body) { return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  function load() { return api("/api/state").then(function (s) { S = s; renderAll(); }); }
  function latestRes() { var m = {}; S.resolutions.resolutions.forEach(function (r) { m[r.finding_id] = r; }); return m; }
  function commentsFor(id) { return S.comments.comments.filter(function (c) { return c.item_id === id; }); }
  function idLink(id) { return el("span", { cls: "iid", tabindex: "0", role: "button", "data-id": id, text: id }); }
  function openFindings() {
    var res = latestRes();
    return S.findings.findings.filter(function (f) { return !res[f.id] || res[f.id].status === "open"; });
  }
  function decisionFor(id) { return (S.decisions || []).filter(function (x) { return x.item_id === id; })[0] || null; }
  function decisionCard(dec) {
    var card = el("div", { cls: "card" });
    var kinds = (dec.suspected_bug ? "suspected bug" : "") + (dec.quirk && dec.suspected_bug ? " · " : "") + (dec.quirk ? "quirk" : "");
    card.appendChild(el("h3", {}, [idLink(dec.item_id), " " + dec.name]));
    card.appendChild(el("div", { cls: "muted", text: kinds + " · migration decision: " + dec.decision + (dec.explicit ? "" : " (default)") + (dec.note ? " - " + dec.note : "") }));
    var note = el("input", { type: "text", placeholder: "Optional note", maxlength: "5000", "aria-label": "Decision note for " + dec.item_id, style: "width:100%;margin-top:10px" });
    note.value = dec.note || "";
    card.appendChild(note);
    var row = el("div", { cls: "row" });
    [["preserve", "Keep as is (preserve)"], ["fix", "Fix it in the migration"]].forEach(function (a) {
      var b = el("button", { type: "button", cls: dec.explicit && dec.decision === a[0] ? "primary" : "", text: a[1] });
      b.addEventListener("click", function () {
        post("/api/decisions", { item_id: dec.item_id, decision: a[0], note: note.value }).then(function () { toast("Saved"); return load(); }).catch(function (e) { toast("Error: " + e.message); });
      });
      row.appendChild(b);
    });
    card.appendChild(row);
    return card;
  }
  function wrap() { var m = document.getElementById("main"); m.textContent = ""; var w = el("div", { cls: "wrap" }); m.appendChild(w); m.scrollTop = 0; return w; }

  function renderTabs() {
    var t = document.getElementById("tabs"); t.textContent = "";
    var n = openFindings().length;
    var tabList = [["doc", "Document"], ["check", "To check"], ["map", "Map"], ["approval", "Approval"]];
    if (S.p2) {
      if (S.p2.fit && S.p2.fit.exists) tabList.push(["fit", "Target fit"]);
      if (S.p2.plan) { tabList.push(["plan", "Plan"]); tabList.push(["slice", "Slice"]); }
      tabList.push(["p2comments", "Comments"]);
    }
    tabList.push(["more", "More"]);
    tabList.forEach(function (x) {
      var b = el("button", { type: "button", cls: tab === x[0] ? "on" : "", text: x[1] });
      if (x[0] === "check" && n) b.appendChild(el("span", { cls: "count", text: String(n) }));
      if (x[0] === "approval" && S.readiness && S.readiness.blockers.length) b.appendChild(el("span", { cls: "count", text: String(S.readiness.blockers.length) }));
      if (x[0] === "fit" && S.p2.fit.blockers && S.p2.fit.blockers.length) b.appendChild(el("span", { cls: "count", text: String(S.p2.fit.blockers.length) }));
      b.addEventListener("click", function () { tab = x[0]; renderAll(); });
      t.appendChild(b);
    });
  }

  function renderDrawer() {
    var d = document.getElementById("drawer"); d.textContent = "";
    if (!selected) { d.style.display = "none"; return; }
    d.style.display = "block";
    var x = el("button", { type: "button", cls: "x", "aria-label": "Close", text: "×" });
    x.addEventListener("click", function () { selected = null; renderDrawer(); });
    d.appendChild(x);
    var dec = decisionFor(selected);
    if (dec) d.appendChild(decisionCard(dec));
    d.appendChild(el("h2", {}, ["Comments on ", idLink(selected)]));
    commentsFor(selected).forEach(function (c) {
      d.appendChild(el("div", { cls: "card" }, [el("div", { cls: "muted", text: c.author + (c.processed ? " · used" : "") }), el("div", { text: c.text })]));
    });
    var ta = el("textarea", { "aria-label": "Comment", maxlength: "5000", placeholder: "What should be different?" });
    var btn = el("button", { type: "button", cls: "primary", text: "Save comment" });
    btn.addEventListener("click", function () {
      var text = ta.value.trim(); if (!text) return;
      post("/api/comments", { item_id: selected, text: text }).then(function () { toast("Saved"); return load(); }).catch(function (e) { toast("Error: " + e.message); });
    });
    d.appendChild(ta); d.appendChild(el("div", { cls: "row" }, [btn]));
  }

  function renderDoc() {
    var w = wrap();
    if (!S.sections.length) { w.appendChild(el("div", { cls: "empty" }, [el("b", { text: "No document yet" }), "Run the analyze command first, then reload."])); return; }
    var sec = S.sections.filter(function (s) { return s.id === secId; })[0] || S.sections[0];
    secId = sec.id;
    var chips = el("div", { cls: "chips" });
    S.sections.forEach(function (s) {
      var b = el("button", { type: "button", cls: s.id === secId ? "on" : "", text: s.title });
      b.addEventListener("click", function () { secId = s.id; renderDoc(); });
      chips.appendChild(b);
    });
    w.appendChild(chips);
    w.appendChild(el("h2", { cls: "title", text: sec.title }));
    var box = el("div"); box.innerHTML = sec.html;   // server-escaped, whitelisted markup only
    w.appendChild(box);
    var ids = {}; S.comments.comments.forEach(function (c) { ids[c.item_id] = 1; });
    box.querySelectorAll(".iid").forEach(function (n) { if (ids[n.getAttribute("data-id")]) n.classList.add("has"); });
  }

  var ANSWERS = [
    ["accepted_doc", "The document is right", true],
    ["accepted_code", "The code is right", false],
    ["dismissed", "Not a real problem", false],
    ["deferred", "Decide during migration (defer)", false]
  ];
  function renderCheck() {
    var w = wrap();
    var ff = S.findings, res = latestRes();
    var list = showAll ? ff.findings : openFindings();
    w.appendChild(el("h2", { cls: "title", text: "To check" }));
    w.appendChild(el("p", { cls: "lead", text: "The AI found places where the document and the code may disagree. Pick the answer that fits." }));
    if (!list.length) {
      w.appendChild(el("div", { cls: "empty" }, [el("b", { text: ff.findings.length ? "All done 🎉" : "Nothing to check" }), ff.findings.length ? "You answered every item." : "Run the verify command to look for problems."]));
    }
    list.forEach(function (f) {
      var cur = res[f.id];
      var card = el("div", { cls: "card" });
      card.appendChild(el("h3", { text: f.title }));
      card.appendChild(el("div", {}, [el("span", { cls: "pill sev-" + f.severity, text: f.severity + " importance" }), cur && cur.status !== "open" ? el("span", { cls: "pill", text: "answered: " + cur.status.replace("_", " ") }) : null]));
      if (f.detail) card.appendChild(el("p", { text: f.detail }));
      f.quotes.slice(0, 2).forEach(function (q) { card.appendChild(el("blockquote", { text: q.text }, [el("div", { text: "from " + q.source })])); });
      if (f.item_ids.length) { var d = el("div", { cls: "muted" }, ["Related: "]); f.item_ids.forEach(function (i) { d.appendChild(idLink(i)); d.appendChild(document.createTextNode(" ")); }); card.appendChild(d); }
      var note = el("input", { type: "text", placeholder: "Optional note", maxlength: "5000", "aria-label": "Note for " + f.title, style: "width:100%;margin-top:10px" });
      card.appendChild(note);
      var row = el("div", { cls: "row" });
      ANSWERS.concat([["needs_reanalysis", "Look again", false]]).forEach(function (a) {
        var b = el("button", { type: "button", cls: a[2] ? "primary" : "", text: a[1] });
        b.addEventListener("click", function () {
          post("/api/resolutions", { finding_id: f.id, status: a[0], note: note.value }).then(function () { toast("Saved"); return load(); }).catch(function (e) { toast("Error: " + e.message); });
        });
        row.appendChild(b);
      });
      card.appendChild(row);
      w.appendChild(card);
    });
    if (ff.findings.length) {
      var t = el("button", { type: "button", text: showAll ? "Hide answered items" : "Show answered items too" });
      t.addEventListener("click", function () { showAll = !showAll; renderCheck(); });
      w.appendChild(t);
    }
  }

  function renderMap() {
    var w = wrap();
    w.appendChild(el("h2", { cls: "title", text: "Map" }));
    w.appendChild(el("p", { cls: "lead", text: "How the parts of the system connect. Drag to move, scroll to zoom." }));
    w.appendChild(el("iframe", { title: "Relationship graph", sandbox: "allow-scripts allow-same-origin", src: BASE + "/graph" }));
  }

  function renderApproval() {
    var w = wrap();
    var r = S.readiness || { ready: false, blockers: [], warnings: [] }, a = S.approval || { state: "none", reason: "" };
    w.appendChild(el("h2", { cls: "title", text: "Approval" }));
    w.appendChild(el("p", { cls: "lead", text: "Approving freezes this understanding as the contract for the migration. Any later re-analysis or doc change makes it stale." }));
    var card = el("div", { cls: "card" });
    card.appendChild(el("h3", { cls: "state-" + a.state, text: "State: " + a.state }));
    card.appendChild(el("div", { cls: "muted", text: a.reason || "" }));
    if (r.blockers.length) {
      card.appendChild(el("h3", { text: "Blockers (" + r.blockers.length + ")" }));
      var ub = el("ul", { cls: "blockers" }); r.blockers.forEach(function (x) { ub.appendChild(el("li", { text: x })); }); card.appendChild(ub);
    } else card.appendChild(el("p", { cls: "added", text: "No blockers: ready to approve." }));
    if (r.warnings.length) {
      card.appendChild(el("h3", { text: "Warnings (" + r.warnings.length + ")" }));
      var uw = el("ul", { cls: "warnings" }); r.warnings.forEach(function (x) { uw.appendChild(el("li", { text: x })); }); card.appendChild(uw);
    }
    var by = el("input", { type: "text", placeholder: "Your name", maxlength: "64", "aria-label": "Approved by", style: "width:100%;margin-top:10px" });
    var note = el("input", { type: "text", placeholder: "Optional note", maxlength: "5000", "aria-label": "Approval note", style: "width:100%;margin-top:10px" });
    var btn = el("button", { type: "button", cls: "primary", text: "Approve" });
    if (r.blockers.length || a.state === "approved") btn.disabled = true;
    btn.addEventListener("click", function () {
      var name = by.value.trim(); if (!name) { toast("Enter your name first"); return; }
      post("/api/approve", { approved_by: name, note: note.value }).then(function () { toast("Approved"); return load(); }).catch(function (e) { toast("Error: " + e.message); });
    });
    card.appendChild(by); card.appendChild(note); card.appendChild(el("div", { cls: "row" }, [btn]));
    w.appendChild(card);
    w.appendChild(el("h3", { text: "Quirks & suspected bugs: migration decisions" }));
    if (!(S.decisions || []).length) w.appendChild(el("p", { cls: "muted", text: "No quirks or suspected bugs in the graph." }));
    (S.decisions || []).forEach(function (d) { w.appendChild(decisionCard(d)); });
  }

  function renderMore() {
    var w = wrap();
    w.appendChild(el("h2", { cls: "title", text: "More" }));
    var c = S.coverage;
    w.appendChild(el("h3", { text: "Numbers" }));
    var g = el("div", { cls: "grid" });
    var all = { "parts": c.node_total, "links": c.edge_total };
    Object.keys(c.nodes_by_type).sort().forEach(function (k) { all[k] = c.nodes_by_type[k]; });
    Object.keys(all).forEach(function (k) { g.appendChild(el("div", { cls: "stat" }, [el("b", { text: String(all[k]) }), k])); });
    w.appendChild(g);
    w.appendChild(el("h3", { text: "What changed between versions" }));
    if (S.versions.length < 2) { w.appendChild(el("p", { cls: "muted", text: "Only one version so far. Changes appear after you re-run the analysis." })); return; }
    var a = el("select", { "aria-label": "From version" }), b = el("select", { "aria-label": "To version" });
    S.versions.forEach(function (v, i) {
      var oa = el("option", { value: String(v), text: "version " + v }), ob = el("option", { value: String(v), text: "version " + v });
      if (i === S.versions.length - 2) oa.selected = true; if (i === S.versions.length - 1) ob.selected = true;
      a.appendChild(oa); b.appendChild(ob);
    });
    var out = el("div");
    var go = el("button", { type: "button", cls: "primary", text: "Compare" });
    go.addEventListener("click", function () {
      api("/api/diff?a=" + encodeURIComponent(a.value) + "&b=" + encodeURIComponent(b.value)).then(function (d) {
        out.textContent = "";
        [["added_nodes", "Added", "added"], ["removed_nodes", "Removed", "removed"], ["changed_nodes", "Changed", "changed"]].forEach(function (x) {
          out.appendChild(el("h3", { cls: x[2], text: x[1] + " (" + d[x[0]].length + ")" }));
          var ul = el("ul"); d[x[0]].forEach(function (n) { ul.appendChild(el("li", { text: n.name })); });
          out.appendChild(ul);
        });
      }).catch(function (e) { toast("Error: " + e.message); });
    });
    w.appendChild(el("div", { cls: "row" }, [a, "to", b, go]));
    w.appendChild(out);
  }


  // ---- Phase 2 tabs (shown only when Phase 2 files exist). Everything is rendered with textContent. ----
  var sliceId = null;
  function pre(obj) { return el("pre", { text: typeof obj === "string" ? obj : JSON.stringify(obj, null, 2) }); }
  function input(ph, label, max) { return el("input", { type: "text", placeholder: ph, maxlength: String(max || 5000), "aria-label": label, style: "width:100%;margin-top:8px" }); }
  function fail(e) { toast("Error: " + e.message); }
  function table(head, rows) {
    var t = el("table", { style: "width:100%;border-collapse:collapse;font-size:13px" });
    var tr = el("tr"); head.forEach(function (h) { tr.appendChild(el("th", { text: h, style: "text-align:left;border-bottom:1px solid var(--border);padding:4px" })); }); t.appendChild(tr);
    rows.forEach(function (r) { var x = el("tr"); r.forEach(function (c) { var td = el("td", { style: "border-bottom:1px solid var(--border);padding:4px;vertical-align:top" }); td.appendChild(typeof c === "object" && c !== null && c.nodeType ? c : document.createTextNode(c == null ? "" : String(c))); x.appendChild(td); }); t.appendChild(x); });
    return t;
  }
  function list(items, cls) { var u = el("ul", cls ? { cls: cls } : {}); items.forEach(function (x) { u.appendChild(el("li", { text: String(x) })); }); return u; }

  function renderFit() {
    var w = wrap(), f = S.p2.fit;
    w.appendChild(el("h2", { cls: "title", text: "Target fit" }));
    w.appendChild(el("p", { cls: "lead", text: "Constructs that do not map directly to the target. Open high-severity risks block planning." }));
    if (f.error) w.appendChild(el("p", { cls: "removed", text: f.error }));
    if (f.stale && f.stale.length) w.appendChild(list(f.stale, "warnings"));
    f.risks.forEach(function (r) {
      var card = el("div", { cls: "card" });
      card.appendChild(el("h3", {}, [el("span", { cls: "pill sev-" + r.severity, text: r.severity }), r.id + " " + r.title]));
      card.appendChild(el("div", { cls: "muted", text: r.kind + (r.blocking ? " · blocking" : "") + (r.resolution ? " · resolved: " + r.resolution.status + " (" + r.resolution.by + ")" : " · open") }));
      if (r.detail) card.appendChild(el("p", { text: r.detail }));
      if (r.proposed_handling) card.appendChild(el("p", { cls: "muted", text: "Proposed: " + r.proposed_handling }));
      (r.evidence || []).slice(0, 3).forEach(function (e) { card.appendChild(el("blockquote", { text: (e.quote || "") }, [el("div", { text: e.file + ":" + e.line_start })])); });
      if (r.item_ids.length) { var d = el("div", { cls: "muted" }, ["Related: "]); r.item_ids.forEach(function (i) { d.appendChild(idLink(i)); d.appendChild(document.createTextNode(" ")); }); card.appendChild(d); }
      var sel = el("select", { "aria-label": "Resolution for " + r.id });
      f.statuses.forEach(function (st) { sel.appendChild(el("option", { value: st, text: st })); });
      var note = input("Why (required)", "Resolution note for " + r.id), by = input("Your name", "Resolved by", 64);
      var b = el("button", { type: "button", cls: "primary", text: "Resolve" });
      b.addEventListener("click", function () {
        if (!note.value.trim()) { toast("A note is required"); return; }
        post("/api/p2/resolve-risk", { risk_id: r.id, status: sel.value, note: note.value, by: by.value.trim() || "user" }).then(function () { toast("Saved"); return load(); }).catch(fail);
      });
      card.appendChild(note); card.appendChild(by); card.appendChild(el("div", { cls: "row" }, [sel, b]));
      w.appendChild(card);
    });
    if (!f.risks.length) w.appendChild(el("div", { cls: "empty" }, [el("b", { text: "No risks" }), "The fit analysis found nothing to decide."]));
  }

  function renderPlan() {
    var w = wrap(), p = S.p2.plan;
    w.appendChild(el("h2", { cls: "title", text: "Plan (v" + p.version + ")" }));
    w.appendChild(el("p", { cls: "lead", text: p.slices.length + " slices, estimated $" + p.estimate_usd + ". Click a slice to review it." }));
    w.appendChild(table(["#", "Slice", "Name", "Status", "Depends on", "Blockers", "Est. $", "Equivalence"], p.slices.map(function (x) {
      var a = el("button", { type: "button", text: x.id });
      a.addEventListener("click", function () { sliceId = x.id; tab = "slice"; renderAll(); });
      return [x.order, a, x.name + " (" + x.kind + ")", x.status + (x.reason ? ": " + x.reason : ""), (x.depends_on || []).join(", "), (x.blockers || []).join(", "), x.estimate_usd,
        x.equivalence_pct == null ? "-" : x.equivalence_pct + "% (" + x.unexplained + " unexplained)"];
    })));
  }

  function commentForm(items, after) {
    var box = el("div", { cls: "card" });
    box.appendChild(el("h3", { text: "Add a comment" }));
    var sel = el("select", { "aria-label": "Comment on" });
    items.forEach(function (i) { sel.appendChild(el("option", { value: i, text: i })); });
    var free = input("or type an ID (SLICE-, CT-, EQ-, RISK-, graph ID)", "Item ID", 64);
    var file = input("target_code file (optional)", "File", 500), line = input("line (optional)", "Line", 9);
    var ta = el("textarea", { "aria-label": "Comment text", maxlength: "5000", placeholder: "What should change?" });
    var b = el("button", { type: "button", cls: "primary", text: "Save comment" });
    b.addEventListener("click", function () {
      var body = { item_id: (free.value.trim() || sel.value), text: ta.value.trim() };
      if (!body.item_id || !body.text) { toast("Item and text are required"); return; }
      if (file.value.trim()) body.file = file.value.trim();
      if (line.value.trim()) { var n = parseInt(line.value, 10); if (!(n > 0)) { toast("Line must be a number"); return; } body.line = n; }
      post("/api/p2/comments", body).then(function () { toast("Saved"); return after(); }).catch(fail);
    });
    [sel, free, file, line, ta].forEach(function (x) { box.appendChild(x); });
    box.appendChild(el("div", { cls: "row" }, [b]));
    return box;
  }
  function commentList(cs) {
    if (!cs.length) return el("p", { cls: "muted", text: "No comments yet." });
    return table(["ID", "On", "Where", "Text", "By", "Used"], cs.map(function (c) { return [c.id, c.item_id, c.file ? c.file + (c.line ? ":" + c.line : "") : "", c.text, c.author, c.processed ? "yes" : "no"]; }));
  }

  function renderSlice() {
    var w = wrap(), p = S.p2.plan;
    if (!p || !p.slices.length) { w.appendChild(el("div", { cls: "empty" }, [el("b", { text: "No slices" }), "Run the plan command first."])); return; }
    if (!sliceId) sliceId = p.slices[0].id;
    var sel = el("select", { "aria-label": "Slice" });
    p.slices.forEach(function (x) { var o = el("option", { value: x.id, text: x.id + " " + x.name + " (" + x.status + ")" }); if (x.id === sliceId) o.selected = true; sel.appendChild(o); });
    sel.addEventListener("change", function () { sliceId = sel.value; renderSlice(); });
    w.appendChild(el("div", { cls: "row" }, [sel]));
    var body = el("div"); w.appendChild(body);
    body.appendChild(el("p", { cls: "muted", text: "Loading..." }));
    api("/api/p2/slice?id=" + encodeURIComponent(sliceId)).then(function (d) { drawSlice(body, d); }).catch(function (e) { body.textContent = ""; body.appendChild(el("p", { cls: "removed", text: "Error: " + e.message })); });
  }

  function drawSlice(body, d) {
    body.textContent = "";
    var sid = d.slice.id;
    body.appendChild(el("h2", { cls: "title", text: sid + " " + d.slice.name }));
    body.appendChild(el("p", { cls: "lead", text: "Status: " + d.state.status + " · iteration " + d.state.iteration + (d.state.reason ? " · " + d.state.reason : "") }));
    var card = el("div", { cls: "card" });
    card.appendChild(el("h3", { text: d.blockers.length ? "Approval blockers (" + d.blockers.length + ")" : "No blockers: ready to approve" }));
    if (d.blockers.length) card.appendChild(list(d.blockers, "blockers"));
    if (d.approval) card.appendChild(el("p", { cls: "added", text: "Approved by " + d.approval.approved_by + " at " + d.approval.approved_at + (d.approval.commit_sha ? " (" + d.approval.commit_sha.slice(0, 12) + ")" : "") }));
    var by = input("Your name", "Your name", 64), note = input("Note (approval) or reason (rejection)", "Note or reason");
    var ok = el("button", { type: "button", cls: "primary", text: "Approve slice" }), no = el("button", { type: "button", text: "Reject slice" });
    if (d.blockers.length) ok.disabled = true;
    ok.addEventListener("click", function () {
      if (!by.value.trim()) { toast("Enter your name first"); return; }
      post("/api/p2/approve-slice", { slice_id: sid, approved_by: by.value.trim(), note: note.value }).then(function () { toast("Approved"); return load(); }).catch(fail);
    });
    no.addEventListener("click", function () {
      if (!by.value.trim() || !note.value.trim()) { toast("Name and reason are required"); return; }
      post("/api/p2/reject-slice", { slice_id: sid, by: by.value.trim(), reason: note.value }).then(function () { toast("Rejected"); return load(); }).catch(fail);
    });
    card.appendChild(by); card.appendChild(note); card.appendChild(el("div", { cls: "row" }, [ok, no]));
    body.appendChild(card);

    body.appendChild(el("h3", { text: "Iterations" }));
    body.appendChild(d.iterations.length ? table(["#", "Build", "Tests", "Files"], d.iterations.map(function (i) { return [i.iteration, i.build == null ? "-" : (i.build ? "green" : "red"), i.tests == null ? "-" : (i.tests ? "pass" : "fail"), i.files.join(", ")]; })) : el("p", { cls: "muted", text: "No iterations yet." }));
    if (d.tests) { body.appendChild(el("h3", { text: "Latest test results" })); body.appendChild(pre(d.tests)); }
    if (d.build) { body.appendChild(el("h3", { text: "Latest build" })); body.appendChild(pre(d.build)); }

    body.appendChild(el("h3", { text: "Code diff (target_code)" }));
    if (d.diff.error) body.appendChild(el("p", { cls: "muted", text: d.diff.error }));
    else {
      body.appendChild(el("p", { cls: "muted", text: "From " + d.diff.base_label + " (" + d.diff.base.slice(0, 12) + ") to HEAD " + d.diff.head.slice(0, 12) + (d.diff.truncated ? " · truncated" : "") }));
      body.appendChild(pre(d.diff.stat || "(no changes)"));
      if (d.diff.diff) body.appendChild(pre(d.diff.diff));
    }
    if (d.human_commits.length) { body.appendChild(el("h3", { text: "Human commits" })); body.appendChild(table(["Commit", "Author", "Subject"], d.human_commits.map(function (c) { return [c.sha.slice(0, 12), c.author, c.subject]; }))); }

    body.appendChild(el("h3", { text: "Equivalence" }));
    var eq = d.equivalence;
    if (!eq) body.appendChild(el("p", { cls: "muted", text: "Not run yet." }));
    else {
      body.appendChild(el("p", { text: eq.cases_equivalent + "/" + eq.cases_total + " cases equivalent (" + eq.equivalence_pct + "%)" }));
      var rows = eq.diffs.map(function (x) {
        var act = "";
        if (x.classification === "unknown" || x.classification === "regression") {
          act = el("button", { type: "button", text: "Approve mask" });
          act.addEventListener("click", function () {
            var who = by.value.trim(); if (!who) { toast("Enter your name above first"); return; }
            post("/api/p2/approve-mask", { eq_id: x.id, by: who, reason: note.value }).then(function () { toast("Mask approved; re-run equivalence"); renderSlice(); }).catch(fail);
          });
        }
        return [x.id, x.case_id, x.field, x.classification + (x.decision_ref ? " (" + x.decision_ref + ")" : ""), JSON.stringify(x.legacy), JSON.stringify(x.new), (x.triage ? x.triage + ": " + x.triage_quote : "") + (x.note ? " " + x.note : ""), act];
      });
      body.appendChild(rows.length ? table(["EQ", "Case", "Field", "Class", "Legacy", "New", "Triage / note", ""], rows) : el("p", { cls: "added", text: "No differences." }));
      if (Object.keys(eq.untestable || {}).length) { body.appendChild(el("h3", { text: "Untestable items" })); body.appendChild(list(Object.keys(eq.untestable).map(function (k) { return k + ": " + eq.untestable[k]; }))); }
    }
    if (d.fix_requests.requests.length) { body.appendChild(el("h3", { text: "Fix requests" })); body.appendChild(table(["EQ", "Case", "Field", "Reason", "Status"], d.fix_requests.requests.map(function (r) { return [r.eq_id, r.case_id, r.field, r.reason, r.status]; }))); }
    if (d.decisions.decisions.length) { body.appendChild(el("h3", { text: "Decisions" })); body.appendChild(table(["Kind", "Ref", "Status", "Choice", "Question / note"], d.decisions.decisions.map(function (x) { return [x.kind, x.ref, x.status, x.choice, x.question || x.note]; }))); }
    if (d.masks.length) { body.appendChild(el("h3", { text: "Masks" })); body.appendChild(table(["EQ", "Case", "Mask", "Status", "By"], d.masks.map(function (m) { return [m.eq_id, m.case_id, m.mask, m.status, m.approved_by || m.proposed_by]; }))); }
    if (d.review.length) { body.appendChild(el("h3", { text: "Reviewer findings" })); body.appendChild(pre(d.review)); }
    if (d.slice.blockers.length) { body.appendChild(el("h3", { text: "Plan blockers" })); body.appendChild(list(d.slice.blockers)); }

    body.appendChild(el("h3", { text: "Comments" }));
    body.appendChild(commentList(d.comments));
    var items = [sid].concat(d.cases.map(function (c) { return c.id; }), eq ? eq.diffs.map(function (x) { return x.id; }) : []);
    body.appendChild(commentForm(items, function () { return load().then(function () { tab = "slice"; renderAll(); }); }));
  }

  function renderP2Comments() {
    var w = wrap();
    w.appendChild(el("h2", { cls: "title", text: "Migration comments" }));
    w.appendChild(el("p", { cls: "lead", text: "Comments on slices, test cases, equivalence diffs, risks or code lines. The next fix iteration reads unprocessed comments." }));
    w.appendChild(commentList(S.p2.comments));
    w.appendChild(commentForm(S.p2.known_ids, load));
  }

  function renderAll() {
    document.getElementById("pname").textContent = (S.project || "Project") + " review";
    renderTabs(); renderDrawer();
    if (tab === "check") renderCheck(); else if (tab === "map") renderMap(); else if (tab === "approval") renderApproval(); else if (tab === "more") renderMore();
    else if (tab === "fit" && S.p2) renderFit(); else if (tab === "plan" && S.p2) renderPlan(); else if (tab === "slice" && S.p2) renderSlice();
    else if (tab === "p2comments" && S.p2) renderP2Comments(); else renderDoc();
  }

  // ---- first-run tour ----
  var step = 0;
  function seen() { try { return localStorage.getItem("ma-tour-done") === "1"; } catch (e) { return false; } }
  function markSeen() { try { localStorage.setItem("ma-tour-done", "1"); } catch (e) {} }
  function closeTour() { document.getElementById("tour").style.display = "none"; markSeen(); }
  function renderTour() {
    var t = document.getElementById("tour"); t.textContent = ""; t.style.display = "flex";
    var s = STEPS[step], last = step === STEPS.length - 1;
    var dots = el("div", { cls: "dots" }); STEPS.forEach(function (_, i) { dots.appendChild(el("i", { cls: i === step ? "on" : "" })); });
    var skip = el("button", { type: "button", text: last ? "Back" : "Skip" });
    skip.addEventListener("click", function () { if (last) { step--; renderTour(); } else closeTour(); });
    var next = el("button", { type: "button", cls: "primary", text: last ? "Start" : "Next" });
    next.addEventListener("click", function () { if (last) closeTour(); else { step++; renderTour(); } });
    t.appendChild(el("div", { cls: "box" }, [el("div", { cls: "big", text: s[0] }), el("h2", { text: s[1] }), el("p", { text: s[2] }), dots, el("div", { cls: "btns" }, [skip, next])]));
    next.focus();
  }
  function startTour() { step = 0; renderTour(); }

  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest(".iid") : null;
    if (t) { selected = t.getAttribute("data-id"); renderDrawer(); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.classList && e.target.classList.contains("iid")) e.target.click();
    if (e.key === "Escape") { closeTour(); }
  });
  document.getElementById("help").addEventListener("click", startTour);
  document.getElementById("theme").addEventListener("click", function () {
    var r = document.documentElement, n = r.getAttribute("data-theme") === "dark" ? "light" : "dark";
    r.setAttribute("data-theme", n); try { localStorage.setItem("lavish-theme", n); } catch (e) {}
  });
  load().then(function () { if (!seen()) startTour(); if (S.findings.findings.length && openFindings().length && !seen()) {} })
    .catch(function (e) { document.getElementById("main").textContent = "Failed to load: " + e.message; });
})();
</script>
</body>
</html>
"""
