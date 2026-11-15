#!/usr/bin/env python3
"""Render every docs/**/*.md into the shared 'lavish' HTML template under docs/html/.

    pip install markdown            # the only dependency (not a project dependency)
    python scripts/build_docs_html.py

Output is fully self-contained (inline CSS/JS, no network): one page per Markdown file plus docs/html/index.html,
sidebar navigation across all docs, an in-page table of contents with scroll-spy, copy buttons, light/dark theme.
Relative links between docs are rewritten from .md to .html. ```mermaid blocks are shown as labelled diagram source
(rendering Mermaid needs a script from a CDN, which this output deliberately avoids).
The hand-built pages (tutorial, user guide, developer handbook) are linked from the index, not regenerated.
"""
from __future__ import annotations

import html
import posixpath
import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:  # pragma: no cover
    sys.exit("pip install markdown   (needed only to build the HTML docs)")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "html"

GROUPS = [
    ("Start here", ["README", "user-guide", "architecture", "api-reference", "deployment", "threat-model"]),
    ("Phase 2: migrate", ["phase2-architecture", "phase2-plan"]),
    ("Roadmap", ["remaining-work"]),
    ("Product and platform", ["product-architecture", "platform-contract", "phase0-results", "operations-runbook"]),
    ("Security", ["security/security-overview-draft", "security/threat-model-hosted", "security/data-handling-and-retention",
                  "security/pentest-2026-11-09", "security/incident-response", "security/secure-sdlc",
                  "security/soc2-readiness", "security/subprocessors"]),
]
HAND_BUILT = [
    ("../developer-handbook.html", "Developer handbook", "Everything a new developer needs, in depth"),
    ("../user-guide-app.html", "Product user guide", "How to use the hosted app, step by step"),
    ("../tutorial.html", "CLI tutorial", "From install to a reviewed result"),
]

CSS = (ROOT / "scripts" / "docs_theme.css").read_text()   # the user-guide-app.html design, shared by every doc

JS = """
(function(){
  var root=document.documentElement,btn=document.getElementById('theme');
  try{var t=localStorage.getItem('ma-guide-theme');if(t==='light'||t==='dark')root.setAttribute('data-theme',t);}catch(e){}
  if(btn)btn.addEventListener('click',function(){
    var cur=root.getAttribute('data-theme');
    if(!cur)cur=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
    var next=cur==='dark'?'light':'dark';root.setAttribute('data-theme',next);
    try{localStorage.setItem('ma-guide-theme',next);}catch(e){}
  });
  document.querySelectorAll('.pw,.cb').forEach(function(w){
    var pre=w.querySelector('pre');if(!pre)return;
    var b=document.createElement('button');b.type='button';b.className='copy';b.textContent='Copy';
    b.setAttribute('aria-label','Copy code to clipboard');
    b.addEventListener('click',function(){
      var txt=pre.innerText;
      var done=function(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1500);};
      if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,function(){});}
      else{try{var r=document.createRange();r.selectNodeContents(pre);var s=window.getSelection();s.removeAllRanges();s.addRange(r);document.execCommand('copy');s.removeAllRanges();done();}catch(e){}}
    });
    w.appendChild(b);
  });
  document.querySelectorAll('.tw,.dia').forEach(function(el){el.setAttribute('tabindex','0');el.setAttribute('role','region');if(!el.getAttribute('aria-label'))el.setAttribute('aria-label','Scrollable content');});
  var links=[].slice.call(document.querySelectorAll('.side a.toc'));
  var map={};links.forEach(function(a){map[a.getAttribute('href').slice(1)]=a;});
  if(links.length&&'IntersectionObserver' in window){
    var io=new IntersectionObserver(function(es){es.forEach(function(e){
      if(e.isIntersecting){links.forEach(function(l){l.classList.remove('on');l.removeAttribute('aria-current');});
        var a=map[e.target.id];if(a){a.classList.add('on');a.setAttribute('aria-current','true');}}});
    },{rootMargin:'-10% 0px -80% 0px'});
    document.querySelectorAll('main h2[id],main h3[id]').forEach(function(h){io.observe(h);});
  }
  document.querySelectorAll('.mnav a').forEach(function(a){a.addEventListener('click',function(){document.querySelector('.mnav').removeAttribute('open');});});
})();
"""


def slug_of(p: Path) -> str:
    return p.relative_to(DOCS).with_suffix("").as_posix()


def title_of(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return re.sub(r"[`*_]", "", m.group(1)).strip() if m else fallback


def rel(from_slug: str, to_slug: str) -> str:
    return posixpath.relpath(to_slug + ".html", posixpath.dirname(from_slug) or ".")


def fix_links(body: str, slug: str, known: set[str]) -> str:
    def repl(m: re.Match) -> str:
        href = m.group(1)
        if re.match(r"^(https?:|mailto:|#)", href):
            return m.group(0)
        path, _, frag = href.partition("#")
        if path.endswith(".md"):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(slug), path[:-3]))
            if target in known:
                return f'href="{rel(slug, target)}{"#" + frag if frag else ""}"'
        elif path:  # a repo file outside docs/: point at the file relative to docs/html
            tgt = posixpath.normpath(posixpath.join("docs", posixpath.dirname(slug), path))
            return f'href="{posixpath.relpath(tgt, posixpath.join("docs/html", posixpath.dirname(slug)))}{"#" + frag if frag else ""}"'
        return m.group(0)
    return re.sub(r'href="([^"]*)"', repl, body)


def render(md_text: str) -> tuple[str, list[tuple[int, str, str]]]:
    md = markdown.Markdown(extensions=["fenced_code", "tables", "toc", "sane_lists", "attr_list", "md_in_html"],
                           extension_configs={"toc": {"permalink": False}})
    body = md.convert(md_text)
    body = re.sub(r"<table>", '<div class="tw"><table>', body)
    body = body.replace("</table>", "</table></div>")
    body = re.sub(r"<blockquote>\s*(.*?)\s*</blockquote>", r'<div class="call">\1</div>', body, flags=re.S)

    def code(m: re.Match) -> str:
        cls = m.group(1) or ""
        inner = m.group(2)
        if "mermaid" in cls:
            return f'<div class="cb mm"><span class="lbl">diagram source (mermaid)</span><pre><code>{inner}</code></pre></div>'
        return f'<div class="pw"><pre><code{(" " + cls) if cls else ""}>{inner}</code></pre></div>'
    body = re.sub(r'<pre><code(?: class="([^"]*)")?>(.*?)</code></pre>', code, body, flags=re.S)
    toc = [(int(m.group(1)), m.group(2), re.sub(r"<[^>]+>", "", m.group(3)))
           for m in re.finditer(r'<h([23]) id="([^"]+)">(.*?)</h\1>', body, re.S)]
    return body, toc


def page(title: str, slug: str, body: str, toc: list, nav: str, prev_next: str, depth_to_root: str,
         group: str = "Documentation") -> str:
    toc_html = "".join(f'<a class="toc{" sub" if lvl == 3 else ""}" href="#{i}">{html.escape(t)}</a>' for lvl, i, t in toc)
    side_toc = f'<b class="h toc-h">On this page</b>{toc_html}' if toc else ""
    lede = ("Generated from the Markdown in <code>docs/" + html.escape(slug) + ".md</code>." if slug != "index"
            else "All project documentation in one place.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} | Parity docs</title><style>{CSS}</style></head><body>
<a class="skip" href="#main">Skip to the content</a>
<header class="hero"><div class="hero-in">
<div class="topbar"><span class="brand"><b>Parity</b> / docs</span><button class="btn" id="theme" type="button" aria-label="Toggle light and dark theme">Toggle theme</button></div>
<span class="eyebrow">{html.escape(group)}</span>
<h1 style="max-width:none">{html.escape(title)}</h1>
<p class="lede">{lede}</p>
</div></header>
<div class="layout" id="top">
<aside class="side" aria-label="Documentation">{nav}{side_toc}</aside>
<main id="main"><details class="mnav"><summary>All docs and this page</summary><nav>{nav}</nav></details>{body}{prev_next}</main>
</div>
<footer>Generated from <code>docs/{html.escape(slug)}.md</code> by <code>scripts/build_docs_html.py</code>. Edit the Markdown, not this file.</footer>
<script>{JS}</script></body></html>"""


def main() -> None:
    files = sorted(DOCS.rglob("*.md"))
    files = [f for f in files if OUT not in f.parents]
    known = {slug_of(f) for f in files}
    titles = {slug_of(f): title_of(f.read_text(), slug_of(f)) for f in files}
    order = [s for _, ss in GROUPS for s in ss if s in known]
    order += sorted(known - set(order))

    def nav_for(cur: str) -> str:
        out = ['<b class="h">Documentation</b>', f'<a href="{rel(cur, "index")}">Overview</a>']
        for name, slugs in GROUPS + [("Other", sorted(known - {s for _, ss in GROUPS for s in ss}))]:
            items = [s for s in slugs if s in known]
            if not items:
                continue
            out.append(f'<div class="grp">{html.escape(name)}</div>')
            for s in items:
                on = ' class="on"' if s == cur else ""
                out.append(f'<a{on} href="{rel(cur, s)}">{html.escape(titles[s])}</a>')
        out.append('<div class="grp">Hand-built guides</div>')
        for href, name, _ in HAND_BUILT:
            prefix = "../" * cur.count("/")
            out.append(f'<a href="{prefix}{href}">{html.escape(name)}</a>')
        return "".join(out)

    OUT.mkdir(exist_ok=True)
    for i, f in enumerate(files):
        slug = slug_of(f)
        body, toc = render(f.read_text())
        body = fix_links(body, slug, known | {"index"})
        k = order.index(slug)
        pn = ""
        if k > 0:
            p = order[k - 1]
            pn += f'<a href="{rel(slug, p)}">&larr; {html.escape(titles[p])}</a>'
        else:
            pn += "<span></span>"
        if k < len(order) - 1:
            n = order[k + 1]
            pn += f'<a href="{rel(slug, n)}">{html.escape(titles[n])} &rarr;</a>'
        target = OUT / (slug + ".html")
        target.parent.mkdir(parents=True, exist_ok=True)
        grp = next((n for n, ss in GROUPS if slug in ss), "Documentation")
        target.write_text(page(titles[slug], slug, body, toc, nav_for(slug), f'<div class="pager">{pn}</div>', "", grp))

    cards = "".join(f'<a class="card" href="{h}"><b>{html.escape(n)}</b><span>{html.escape(d)}</span></a>' for h, n, d in HAND_BUILT)
    for name, slugs in GROUPS:
        items = [s for s in slugs if s in known]
        cards += "".join(f'<a class="card" href="{rel("index", s)}"><b>{html.escape(titles[s])}</b><span>{html.escape(name)}</span></a>' for s in items)
    other = sorted(known - {s for _, ss in GROUPS for s in ss})
    cards += "".join(f'<a class="card" href="{rel("index", s)}"><b>{html.escape(titles[s])}</b><span>Other</span></a>' for s in other)
    intro = ("<p>All project documentation in one place. The hand-built guides come first; the rest is rendered from the "
             "Markdown files in <code>docs/</code>.</p><div class=\"cards\">" + cards + "</div>")
    (OUT / "index.html").write_text(page("Documentation", "index", intro, [], nav_for("index"), "", ""))
    print(f"wrote {len(files)} pages + index to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
