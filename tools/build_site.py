#!/usr/bin/env python3
"""Build the MPXAir documentation website.

    python3 tools/build_site.py            # writes website/_site/
    python3 -m http.server -d website/_site 8000   # preview

Each file in website/pages/ is an HTML fragment that starts with a header:

    <!--
    title: Fly screen
    section: Using MPXAir
    order: 40
    lead: One-sentence summary shown under the title.
    -->

The script wraps every page in the shared layout (header, sidebar, contents list,
previous/next links) and writes a search index. No dependencies.
"""
import html
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "website", "pages")
ASSETS = os.path.join(ROOT, "website", "assets")
OUT = os.path.join(ROOT, "website", "_site")
SECTIONS = ["Getting started", "Connecting", "Using MPXAir", "Ground station", "Hardware and integration", "Help"]
REPO = "https://github.com/narl3yyy-svg/MPXAir"

ICON_SEARCH = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/>'
               '<path d="m20 20-3.5-3.5"/></svg>')
ICON_THEME = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3 '
              '7 7 0 0 0 21 12.8z"/></svg>')
ICON_MENU = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"/>'
             '</svg>')
ICON_GH = ('<svg viewBox="0 0 16 16" width="18" height="18" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 '
           '2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13'
           '-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64'
           '-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 '
           '1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 '
           '1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>')


def read_pages():
    pages = []
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".html"):
            continue
        with open(os.path.join(SRC, fn), encoding="utf-8") as fh:
            text = fh.read()
        m = re.match(r"\s*<!--(.*?)-->", text, re.S)
        meta = {}
        if m:
            for line in m.group(1).strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            text = text[m.end():]
        meta["file"] = fn
        meta["body"] = text.strip()
        meta["order"] = float(meta.get("order", 999))
        meta.setdefault("section", "Help")
        meta.setdefault("title", fn[:-5])
        pages.append(meta)
    pages.sort(key=lambda p: (SECTIONS.index(p["section"]) if p["section"] in SECTIONS else 99, p["order"]))
    return pages


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def add_heading_ids(body):
    def rep(m):
        tag, attrs, inner = m.group(1), m.group(2), m.group(3)
        if "id=" in attrs:
            return m.group(0)
        plain = re.sub(r"<[^>]+>", "", inner)
        return f'<{tag}{attrs} id="{slug(plain)}">{inner}</{tag}>'
    return re.sub(r"<(h[23])([^>]*)>(.*?)</\1>", rep, body, flags=re.S)


def plain_text(fragment):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def sidebar(pages, current):
    out = []
    for sec in SECTIONS:
        items = [p for p in pages if p["section"] == sec]
        if not items:
            continue
        out.append(f"<h4>{html.escape(sec)}</h4>")
        for p in items:
            cls = ' class="active"' if p is current else ""
            out.append(f'<a href="{p["file"]}"{cls}>{html.escape(p.get("nav", p["title"]))}</a>')
    return "\n".join(out)


def render(page, pages, i):
    prev_p = pages[i - 1] if i > 0 else None
    next_p = pages[i + 1] if i + 1 < len(pages) else None
    pager = '<nav class="pager">'
    pager += (f'<a class="prev" href="{prev_p["file"]}"><small>← Previous</small>{html.escape(prev_p["title"])}</a>'
              if prev_p else "<span></span>")
    pager += (f'<a class="next" href="{next_p["file"]}"><small>Next →</small>{html.escape(next_p["title"])}</a>'
              if next_p else "<span></span>")
    pager += "</nav>"
    body = add_heading_ids(page["body"])
    is_home = page["file"] == "index.html"
    head = "" if is_home else (
        f'<div class="crumbs">{html.escape(page["section"])}</div>'
        f'<h1>{html.escape(page["title"])}</h1>'
        + (f'<p class="lead">{page["lead"]}</p>' if page.get("lead") else ""))
    title = "MPXAir documentation" if is_home else f'{page["title"]} · MPXAir documentation'
    desc = plain_text(page.get("lead", "")) or "MPXAir: a modern ArduPilot ground control station for Linux."
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="icon" href="assets/img/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="assets/style.css">
<script>try{{var t=localStorage.getItem("mpxair-theme");if(t)document.documentElement.setAttribute("data-theme",t)}}catch(e){{}}</script>
</head>
<body>
<header class="topbar">
  <button class="iconbtn menubtn" id="menu" aria-label="Menu">{ICON_MENU}</button>
  <a class="brand" href="index.html"><img src="assets/img/icon.svg" alt=""> MPXAir <small>docs</small></a>
  <div class="search">{ICON_SEARCH}<input id="q" type="search" placeholder="Search the docs" autocomplete="off"
    aria-label="Search the documentation"><kbd>/</kbd><div class="results" id="results"></div></div>
  <button class="iconbtn" id="theme" aria-label="Light or dark">{ICON_THEME}</button>
  <a class="gh" href="{REPO}">{ICON_GH}<span>GitHub</span></a>
</header>
<div class="layout">
  <aside class="sidebar">{sidebar(pages, page)}</aside>
  <main><article>
{head}
{body}
{pager}
<footer class="site">MPXAir is developed by <a href="https://github.com/narl3yyy-svg">narl3yyy-svg</a> ·
GPL-3.0 · <a href="{REPO}">Source on GitHub</a> · <a href="{REPO}/issues">Report a problem</a></footer>
  </article></main>
  <aside class="toc"><h5>On this page</h5><nav></nav></aside>
</div>
<script src="assets/search-index.js"></script>
<script src="assets/app.js"></script>
</body>
</html>
"""


def search_index(pages):
    idx = []
    for p in pages:
        body = add_heading_ids(p["body"])
        parts = re.split(r"(<h[23][^>]*id=\"[^\"]+\"[^>]*>.*?</h[23]>)", body, flags=re.S)
        idx.append({"t": p["title"], "p": "", "u": p["file"], "h": False,
                    "b": plain_text(p.get("lead", "") + " " + parts[0])[:4000]})
        for j in range(1, len(parts), 2):
            m = re.match(r'<h[23][^>]*id="([^"]+)"[^>]*>(.*?)</h[23]>', parts[j], re.S)
            text = plain_text(parts[j + 1] if j + 1 < len(parts) else "")
            idx.append({"t": plain_text(m.group(2)), "p": p["title"], "u": f'{p["file"]}#{m.group(1)}', "h": True,
                        "b": text[:4000]})
    return idx


def main():
    pages = read_pages()
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(ASSETS, os.path.join(OUT, "assets"))
    for i, p in enumerate(pages):
        with open(os.path.join(OUT, p["file"]), "w", encoding="utf-8") as fh:
            fh.write(render(p, pages, i))
    with open(os.path.join(OUT, "assets", "search-index.js"), "w", encoding="utf-8") as fh:
        fh.write("window.MPX_INDEX=" + json.dumps(search_index(pages), separators=(",", ":")) + ";\n")
    with open(os.path.join(OUT, ".nojekyll"), "w") as fh:
        fh.write("")
    print(f"Built {len(pages)} pages into {OUT}")


if __name__ == "__main__":
    sys.exit(main())
