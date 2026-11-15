#!/usr/bin/env python3
"""Inject (or refresh) the shared docs motion into the hand-built HTML pages. Idempotent: it replaces the blocks
between the markers. Generated pages get the same CSS and JS from scripts/build_docs_html.py."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "scripts" / "docs_motion.css").read_text()
JS = (ROOT / "scripts" / "docs_motion.js").read_text()
PAGES = ["docs/user-guide-app.html", "docs/tutorial.html", "docs/developer-handbook.html"]

CSS_RE = re.compile(r"/\* motion:start \*/.*?/\* motion:end \*/\n?", re.S)
JS_RE = re.compile(r"<script data-motion>.*?</script>\n?", re.S)

for rel in PAGES:
    p = ROOT / rel
    s = p.read_text()
    s = CSS_RE.sub("", s)
    s = JS_RE.sub("", s)
    s = s.replace("</style>", "/* motion:start */\n" + CSS + "/* motion:end */\n</style>", 1)
    s = s.replace("</body>", "<script data-motion>\n" + JS + "</script>\n</body>", 1)
    p.write_text(s)
    print("motion applied:", rel)
