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

CSS = """
:root{--bg:#fbfaf7;--panel:#fff;--ink:#1c1b22;--mute:#6b6a76;--line:#e7e4dc;--accent:#5b4bdb;--accent2:#e0568b;--tint:#efecff;--code:#15141c;--codefg:#e9e7f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f0e14;--panel:#17161f;--ink:#ecebf3;--mute:#9a98a8;--line:#2a2836;--accent:#9b8cff;--accent2:#ff7fac;--tint:#221f3a}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent)}
.top{padding:44px 24px 34px;text-align:center;border-bottom:1px solid var(--line);background:radial-gradient(900px 300px at 50% -80px,var(--tint),transparent),var(--bg)}
.top .tag{display:inline-block;padding:3px 12px;border:1px solid var(--line);border-radius:99px;font-size:13px;color:var(--mute);background:var(--panel)}
.top h1{font-size:clamp(28px,5vw,44px);line-height:1.15;margin:14px auto 0;max-width:900px;letter-spacing:-.02em;background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
.layout{display:grid;grid-template-columns:250px minmax(0,1fr) 210px;gap:36px;max-width:1360px;margin:0 auto;padding:32px 24px 90px}
nav.docs,nav.toc{position:sticky;top:20px;align-self:start;max-height:calc(100vh - 40px);overflow:auto;font-size:14px}
nav b{display:block;margin:14px 0 6px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.08em;font-size:11.5px}
nav b:first-child{margin-top:0}
nav a{display:block;padding:4px 10px;border-left:2px solid var(--line);color:var(--mute);text-decoration:none}
nav a:hover,nav a.on{color:var(--accent);border-color:var(--accent)}
nav a.on{font-weight:600}
nav.toc a.l3{padding-left:22px;font-size:13px}
main{min-width:0}main h2{font-size:27px;margin:44px 0 12px;letter-spacing:-.01em;padding-top:6px;border-top:1px solid var(--line)}
main h1{display:none}main h3{font-size:20px;margin:30px 0 8px}main h4{font-size:16px;margin:22px 0 6px}
main p,main li{max-width:78ch}p{margin:0 0 14px}ul,ol{padding-left:24px;margin:0 0 16px}li{margin:4px 0}
code{font:13.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--tint);padding:2px 6px;border-radius:6px}
.cb{position:relative;margin:14px 0 20px}
pre{background:var(--code);color:var(--codefg);padding:16px 18px;border-radius:14px;overflow:auto;margin:0;line-height:1.55;box-shadow:0 8px 24px -14px #0008}
pre code{background:none;padding:0;color:inherit;font-size:13px}
.cb button{position:absolute;top:8px;right:8px;font:12px inherit;padding:3px 9px;border-radius:8px;border:1px solid #fff3;background:#ffffff14;color:#e9e7f5;cursor:pointer}
.cb button:hover{background:#ffffff2a}
.cb .lbl{position:absolute;top:8px;left:14px;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#b9b5d6}
.cb.mm pre{padding-top:34px}
.tw{overflow-x:auto;margin:14px 0 22px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:14.5px}th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);background:var(--tint)}tr:last-child td{border-bottom:0}
blockquote{margin:16px 0;padding:10px 18px;border-left:4px solid var(--accent);background:var(--tint);border-radius:0 12px 12px 0}blockquote p{margin:6px 0}
hr{border:0;border-top:1px solid var(--line);margin:30px 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin:18px 0}
.card{display:block;padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--panel);text-decoration:none;color:var(--ink)}
.card:hover{border-color:var(--accent)}.card b{display:block;color:var(--accent);margin-bottom:2px}.card span{color:var(--mute);font-size:14px}
.pager{display:flex;justify-content:space-between;gap:12px;margin-top:56px;padding-top:20px;border-top:1px solid var(--line);font-size:14px}
footer{text-align:center;color:var(--mute);font-size:13px;padding:28px;border-top:1px solid var(--line)}
details.mob{display:none;margin:0 24px 16px;border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:8px 14px}
@media(max-width:1100px){.layout{grid-template-columns:230px minmax(0,1fr)}nav.toc{display:none}}
@media(max-width:820px){.layout{grid-template-columns:1fr;padding-top:16px}nav.docs{display:none}details.mob{display:block}}
"""

JS = """
(function(){
 document.querySelectorAll('.cb').forEach(function(b){var btn=document.createElement('button');btn.textContent='Copy';
  btn.addEventListener('click',function(){var t=b.querySelector('code').innerText;
   (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){btn.textContent='Copied';setTimeout(function(){btn.textContent='Copy'},1400)},function(){btn.textContent='Press Ctrl/Cmd+C'});});
  b.appendChild(btn);});
 var links=[].slice.call(document.querySelectorAll('nav.toc a'));if(!links.length||!('IntersectionObserver' in window))return;
 var map={};links.forEach(function(a){map[a.getAttribute('href').slice(1)]=a});
 var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){links.forEach(function(a){a.classList.remove('on')});var a=map[e.target.id];if(a)a.classList.add('on');}})},{rootMargin:'0px 0px -75% 0px'});
 document.querySelectorAll('main h2[id],main h3[id]').forEach(function(h){io.observe(h)});
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

    def code(m: re.Match) -> str:
        cls = m.group(1) or ""
        inner = m.group(2)
        if "mermaid" in cls:
            return f'<div class="cb mm"><span class="lbl">diagram source (mermaid)</span><pre><code>{inner}</code></pre></div>'
        return f'<div class="cb"><pre><code{(" " + cls) if cls else ""}>{inner}</code></pre></div>'
    body = re.sub(r'<pre><code(?: class="([^"]*)")?>(.*?)</code></pre>', code, body, flags=re.S)
    toc = [(int(m.group(1)), m.group(2), re.sub(r"<[^>]+>", "", m.group(3)))
           for m in re.finditer(r'<h([23]) id="([^"]+)">(.*?)</h\1>', body, re.S)]
    return body, toc


def page(title: str, slug: str, body: str, toc: list, nav: str, prev_next: str, depth_to_root: str) -> str:
    toc_html = "".join(f'<a class="l{lvl}" href="#{i}">{html.escape(t)}</a>' for lvl, i, t in toc)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} | migration-agent docs</title><style>{CSS}</style></head><body>
<header class="top"><span class="tag">migration-agent docs</span><h1>{html.escape(title)}</h1></header>
<details class="mob"><summary>All docs</summary>{nav}</details>
<div class="layout"><nav class="docs" aria-label="Documentation">{nav}</nav>
<main>{body}{prev_next}</main>
<nav class="toc" aria-label="On this page"><b>On this page</b>{toc_html}</nav></div>
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
        out = [f'<a href="{rel(cur, "index")}">Overview</a>']
        for name, slugs in GROUPS + [("Other", sorted(known - {s for _, ss in GROUPS for s in ss}))]:
            items = [s for s in slugs if s in known]
            if not items:
                continue
            out.append(f"<b>{html.escape(name)}</b>")
            for s in items:
                on = ' class="on"' if s == cur else ""
                out.append(f'<a{on} href="{rel(cur, s)}">{html.escape(titles[s])}</a>')
        out.append("<b>Hand-built guides</b>")
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
        target.write_text(page(titles[slug], slug, body, toc, nav_for(slug), f'<div class="pager">{pn}</div>', ""))

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
