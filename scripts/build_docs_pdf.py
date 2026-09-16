#!/usr/bin/env python3
"""Render the Foreman prose docs to clean PDFs — same Chrome print approach the deck uses.

markdown -> styled HTML (a light, print-friendly theme) -> headless Chrome --print-to-pdf.
Run:  python3 scripts/build_docs_pdf.py  ->  docs/Foreman-<Doc>.pdf
"""
import os
import subprocess

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# (source markdown, output basename, on-page title)
DOCS = [
    ("README.md", "Foreman-README", "Foreman"),
    ("docs/ONE-PAGER.md", "Foreman-One-Pager", "Foreman — one-pager"),
    ("docs/COMPARISON.md", "Foreman-Comparison", "Foreman — comparison"),
    ("docs/COMPETITIVE.md", "Foreman-Competitive", "Foreman — competitive scan"),
]

CSS = """
@page { size: Letter; margin: 0.7in 0.75in; }
* { box-sizing: border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  color:#1f2328; line-height:1.55; font-size:12.5px; -webkit-font-smoothing:antialiased; }
.doc { max-width:7.1in; margin:0 auto; }
h1 { font-size:26px; font-weight:800; margin:0 0 4px; padding-bottom:8px;
  border-bottom:3px solid #7c9cf5; letter-spacing:.2px; }
h2 { font-size:17px; font-weight:750; margin:26px 0 8px; padding-bottom:5px; border-bottom:1px solid #e2e6ea; color:#0b3a8f; }
h3 { font-size:14px; font-weight:700; margin:18px 0 6px; }
p, li { font-size:12.5px; }
ul, ol { margin:6px 0 6px 20px; } li { margin:3px 0; }
a { color:#2b6ce6; text-decoration:none; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px;
  background:#f2f4f7; padding:1px 5px; border-radius:4px; color:#8a1f6b; }
pre { background:#0d1117; color:#e6edf3; border:1px solid #d0d7de; border-radius:8px;
  padding:12px 14px; overflow:auto; line-height:1.45; page-break-inside:avoid; }
pre code { background:none; color:#e6edf3; padding:0; font-size:10.5px; }
blockquote { margin:8px 0; padding:2px 14px; border-left:4px solid #7c9cf5; color:#57606a; background:#f7f9fc; }
table { border-collapse:collapse; width:100%; margin:12px 0; font-size:10.5px; page-break-inside:avoid; }
th, td { border:1px solid #d0d7de; padding:6px 9px; text-align:left; vertical-align:top; }
th { background:#f2f4f7; font-weight:700; }
tr:nth-child(even) td { background:#fafbfc; }
strong { color:#0b1220; }
hr { border:0; border-top:1px solid #e2e6ea; margin:22px 0; }
h1, h2, h3 { page-break-after:avoid; }
"""


def render(src, out_basename, title):
    with open(os.path.join(ROOT, src)) as f:
        text = f.read()
    body = markdown.markdown(text, extensions=["extra", "sane_lists", "toc", "nl2br"])
    html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
            f"<style>{CSS}</style></head><body><div class='doc'>{body}</div></body></html>")
    html_path = os.path.join(OUT_DIR, "_" + out_basename + ".html")
    with open(html_path, "w") as f:
        f.write(html)
    pdf = os.path.join(OUT_DIR, out_basename + ".pdf")
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    "--virtual-time-budget=4000", "--print-to-pdf=" + pdf, "file://" + html_path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(html_path)
    return pdf


if __name__ == "__main__":
    for src, base, title in DOCS:
        if os.path.exists(os.path.join(ROOT, src)):
            print("PDF:", render(src, base, title))
