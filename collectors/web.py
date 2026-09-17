"""A local web dashboard for Foreman -- the brief board in a browser.

Read-only over the index + receipts + registry, with one-click dispatch. Pure stdlib
``http.server`` (no framework, per repo conventions); every section reuses the collector
logic that powers the text brief, so the two never diverge. Serve with:

    python -m collectors.web --port 8787

Then open http://localhost:8787. The page auto-refreshes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

from collectors import (brief, config_resolve, db, decisions as D, discover, dispatch,
                        escalations, loops, quota, recent, repo_health, scheduler, secrets,
                        validate)

CSS = """
:root{--bg:#0d1117;--panel:#151b23;--card:#1a212b;--line:#2a333f;
--fg:#e6edf3;--dim:#9aa7b5;--faint:#6b7684;
--green:#3fb950;--amber:#e3b341;--red:#f85149;--blue:#58a6ff;--accent:#7c9cf5;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,Helvetica,Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
header{padding:16px 28px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;
background:linear-gradient(180deg,#161d27,var(--panel));position:sticky;top:0;z-index:5}
.brand{display:flex;flex-direction:column;gap:3px}
h1{font-size:20px;font-weight:800;margin:0;letter-spacing:1.5px;display:flex;align-items:center;gap:10px;
background:linear-gradient(90deg,var(--fg),var(--accent));-webkit-background-clip:text;
background-clip:text;-webkit-text-fill-color:transparent}
h1::before{content:"";width:10px;height:10px;border-radius:3px;flex:0 0 auto;
background:linear-gradient(135deg,var(--accent),var(--blue));
box-shadow:0 0 12px rgba(124,156,245,.6);-webkit-text-fill-color:initial}
.tagline{color:var(--dim);font-size:12.5px;font-weight:400;letter-spacing:.1px;max-width:660px}
.legend-strip{background:var(--panel);border:1px solid var(--line);border-radius:10px;
margin-bottom:20px;font-size:12.5px}
.legend-strip>summary{cursor:pointer;padding:10px 14px;color:var(--fg);font-weight:600;
list-style:none;display:flex;align-items:center;gap:8px}
.legend-strip>summary::-webkit-details-marker{display:none}
.legend-strip>summary::before{content:"›";display:inline-block;transition:transform .15s;color:var(--dim)}
.legend-strip[open]>summary::before{transform:rotate(90deg)}
.legend-strip>summary .hint{font-weight:400;color:var(--dim)}
.legend-body{padding:2px 14px 14px;display:flex;flex-wrap:wrap;gap:10px 26px}
.legend-body .key{display:flex;gap:16px;align-items:center;flex-wrap:wrap;
padding-bottom:10px;border-bottom:1px solid var(--line);width:100%}
.legend-body .key b{color:var(--fg);font-weight:600}
.legend-body dl{margin:0;display:flex;gap:6px;align-items:baseline;max-width:340px}
.legend-body dt{color:var(--fg);font-weight:600;white-space:nowrap}
.legend-body dd{margin:0;color:var(--dim)}
.tagline b{color:var(--fg);font-weight:600}
.meta{color:var(--dim);font-size:12.5px;text-align:right;line-height:1.7}
.meta .live{color:var(--green);font-weight:600}
.meta .warn{color:var(--amber);font-weight:600}
.wrap{padding:24px 28px;max-width:1180px;margin:0 auto}
section{margin-bottom:34px}
h2{font-size:15px;font-weight:700;color:var(--fg);margin:0;padding:0;border:0;
display:flex;align-items:baseline;gap:10px;letter-spacing:.2px}
h2::before{content:"";width:4px;height:15px;background:var(--hh,var(--accent));border-radius:2px;
align-self:center;flex:0 0 auto;box-shadow:0 0 10px var(--hh,transparent)}
h2 .htitle{color:var(--hh,var(--fg))}
h2 .subhead{font-weight:400;font-size:12.5px;color:var(--dim);letter-spacing:0}
.count-badge{font-weight:600;font-size:11px;padding:1px 8px;border-radius:20px;
background:color-mix(in srgb,var(--hh,var(--accent)) 18%,transparent);
color:var(--hh,var(--accent));margin-left:2px}
.sumline{color:var(--dim);font-size:13px;margin-top:12px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.sumline .n{color:var(--fg);font-weight:600}
.next-fire{font-family:var(--mono);font-size:12.5px;color:var(--green)}
.next-fire .rel{color:var(--dim)}
.stats{display:flex;flex-wrap:wrap;gap:8px;padding:12px 14px}
.stat{min-width:64px;padding:7px 11px;border:1px solid var(--line);border-radius:8px;
background:var(--panel);display:flex;flex-direction:column;gap:1px;line-height:1.25}
.stat .v{font-size:15px;font-weight:700;font-family:var(--mono);color:var(--fg)}
.stat .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--faint)}
.stat.green .v{color:var(--green)}.stat.amber .v{color:var(--amber)}.stat.red .v{color:var(--red)}
.stat.amber{border-color:rgba(227,179,65,.35)}.stat.red{border-color:rgba(248,81,73,.4)}
.sparkline{display:inline-flex;gap:2px;margin-left:8px;vertical-align:middle}
.spark{width:5px;height:13px;border-radius:1px;background:var(--faint);display:inline-block}
.spark.green{background:var(--green)}.spark.amber{background:var(--amber)}
.spark.red{background:var(--red)}.spark.stale{background:var(--dim)}
td.money{text-align:right;font-family:var(--mono);white-space:nowrap}
td.money.zero{color:var(--faint)}
.bar{display:inline-block;height:7px;border-radius:3px;background:var(--accent);vertical-align:middle;min-width:2px}
.activity{display:flex;gap:11px;align-items:baseline;padding:7px 2px;border-bottom:1px solid var(--line);font-size:13px}
.activity:last-child{border-bottom:0}
.activity .ic{width:15px;text-align:center;flex:0 0 auto;color:var(--faint)}
.activity .ic.green{color:var(--green)}.activity .ic.amber{color:var(--amber)}
.activity .ic.red{color:var(--red)}.activity .ic.stale{color:var(--dim)}
.activity .act-body{flex:1 1 auto;min-width:0}
.activity .act-detail{color:var(--dim);font-size:12px;margin-top:2px;line-height:1.45}
.activity .when{color:var(--faint);font-size:12px;white-space:nowrap;min-width:76px;text-align:right;flex:0 0 auto}
.activity.red .act-detail{color:#f0a9a4}.activity.amber .act-detail{color:#e6cf8f}
.activity.isnew{background:rgba(124,156,245,.08);border-radius:6px;padding-left:8px;padding-right:8px}
.actfeed{padding:4px 16px;max-height:300px;overflow-y:auto}
.actfeed::-webkit-scrollbar{width:9px}
.actfeed::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}
.actfeed::-webkit-scrollbar-thumb:hover{background:var(--faint)}
details.sec{margin-bottom:34px}
details.sec>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::before{content:"\\203A";color:var(--dim);font-size:16px;line-height:1;
transition:transform .15s;flex:0 0 auto;width:10px}
details.sec[open]>summary::before{transform:rotate(90deg)}
details.sec>summary:hover .htitle{text-decoration:underline}
details.sec>summary>h2{flex:1 1 auto}
.sec-body{margin-top:14px}
.sec-sum{margin-left:auto;color:var(--dim);font-size:12px;display:flex;gap:11px;
align-items:center;flex:0 0 auto;white-space:nowrap;padding-left:12px}
.sec-sum .n{color:var(--fg);font-weight:600}
.sec-sum .dot{margin-right:3px}
.sec-sum .ok{color:var(--green)}
details.sec[open] .sec-sum{display:none}                    /* peek only while collapsed */
.recent-row{border-bottom:1px solid var(--line)}
.recent-row:last-child{border-bottom:0}
.recent-row>summary,.recent-row.flat{list-style:none;display:flex;align-items:baseline;gap:10px;
padding:7px 10px;cursor:pointer}
.recent-row.flat{cursor:default}
.recent-row>summary::-webkit-details-marker{display:none}
.recent-row>summary::before{content:"\\203A";color:var(--faint);font-size:13px;transition:transform .15s}
.recent-row[open]>summary::before{transform:rotate(90deg)}
.rp-proj{white-space:nowrap;min-width:130px}
.rp-prompt{color:var(--fg);flex:1 1 auto;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.recent-row[open] .rp-prompt{white-space:normal}
.rp-when{white-space:nowrap;color:var(--faint);font-size:12px;margin-left:auto}
ol.timeline{margin:0 0 8px;padding:0 12px 0 40px;display:flex;flex-direction:column;gap:4px}
ol.timeline li{color:var(--dim);font-size:12.5px;line-height:1.4;list-style:none}
ol.timeline li:first-child{color:var(--fg)}
.tl-when{color:var(--faint);font-family:var(--mono);font-size:11px;margin-right:8px}
form.pq{display:flex;gap:8px;align-items:center;margin:0 0 12px}
form.pq input{flex:1 1 auto;max-width:420px;padding:6px 10px}
.pq-clear{color:var(--dim);font-size:12px;text-decoration:none}
.pq-clear:hover{color:var(--fg)}
.week{font-size:12.5px;color:var(--fg);margin:0 0 12px;line-height:1.7}
ol.results{margin:0;padding:0;list-style:none;display:flex;flex-direction:column}
ol.results li{display:flex;gap:10px;align-items:baseline;padding:6px 10px;
border-bottom:1px solid var(--line);font-size:13px}
ol.results li:last-child{border-bottom:0}
.sched{display:flex;flex-direction:column;gap:3px}
.sched form{margin:0;display:flex;align-items:center;gap:4px}
.sched-every{font-size:12px;color:var(--dim)}
.sched-every input{width:44px;padding:2px 5px;font-size:12px;text-align:center}
.sched-every button{padding:2px 8px;font-size:11px}
.sched-every.sched-on{color:var(--fg);font-weight:600}
.sched-named select{font-size:11px;color:var(--dim);padding:2px 4px}
.nextrun{color:var(--green);font-family:var(--mono);font-size:12px}
.nextrun b{color:var(--fg);font-weight:600}
.activity.isnew .when::after{content:" · new";color:var(--accent);font-weight:600}
section>table,section>.card,section>form,section>.legend{margin-top:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
td,th{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
th{color:var(--dim);font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.6px;background:var(--panel)}
td{font-size:13.5px}
tbody tr:hover td{background:rgba(124,156,245,.05)}
td.cell{text-align:center;white-space:nowrap}
td.cell.green{background:rgba(63,185,80,.08)}
td.cell.amber{background:rgba(227,179,65,.10)}
td.cell.red{background:rgba(248,81,73,.11)}
td.cell.stale{background:rgba(154,167,181,.07)}
td.cell .run{margin-left:7px;padding:1px 7px;font-size:11px;opacity:.5;vertical-align:middle}
td.cell:hover .run{opacity:1}
td.proj{font-weight:600;color:var(--fg)}
details.repo-acc{background:var(--card);border:1px solid var(--line);border-radius:10px;
margin-bottom:8px;overflow:hidden}
details.repo-acc>summary{list-style:none;cursor:pointer;padding:11px 14px;display:flex;
align-items:center;gap:10px;user-select:none}
details.repo-acc>summary::-webkit-details-marker{display:none}
details.repo-acc>summary::before{content:"\\25B8";color:var(--dim);font-size:11px;
transition:transform .12s ease}
details.repo-acc[open]>summary::before{transform:rotate(90deg)}
details.repo-acc>summary:hover{background:rgba(124,156,245,.05)}
details.repo-acc>summary .repo{font-weight:600}
details.repo-acc>summary .count{color:var(--dim);font-size:12px;margin-left:auto}
details.repo-acc table{border:0;border-radius:0;margin:0}
details.repo-acc table th{background:var(--bg)}
.sumdots{display:inline-flex;gap:4px;align-items:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--faint)}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}
.dot.red{background:var(--red)}.dot.stale{background:var(--dim)}
.pill{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}
.green{background:rgba(63,185,80,.16);color:var(--green)}
.amber{background:rgba(227,179,65,.16);color:var(--amber)}
.red{background:rgba(248,81,73,.16);color:var(--red)}
.stale{background:rgba(154,167,181,.14);color:var(--dim)}
.muted{color:var(--faint)}
.dash{color:var(--faint)}
.legend{color:var(--dim);font-size:12px}
.banner{background:rgba(227,179,65,.12);color:var(--amber);padding:10px 14px;
border-radius:8px;margin-bottom:16px;border:1px solid rgba(227,179,65,.25)}
.banner.err{background:rgba(248,81,73,.12);color:var(--red);border-color:rgba(248,81,73,.3)}
button{background:rgba(88,166,255,.1);color:var(--blue);border:1px solid rgba(88,166,255,.3);
border-radius:7px;padding:4px 11px;cursor:pointer;font:inherit;font-size:13px;font-weight:500}
button:hover{background:rgba(88,166,255,.18)}
.sev{font-weight:700;margin-right:6px}.na{color:var(--dim)}
.ok-empty{color:var(--green);border-color:rgba(63,185,80,.25);background:rgba(63,185,80,.06)}
.dim{color:var(--dim)}.mono{font-family:var(--mono);font-size:12.5px}
.acts{margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.acts form{margin:0}
.editlink{color:var(--blue);text-decoration:none;font-size:13px}
.editlink:hover{text-decoration:underline}
.loop-editor{display:flex;flex-direction:column;gap:9px;margin:0}
.loop-editor label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--dim)}
.loop-editor input,.loop-editor textarea{background:var(--panel);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:5px 8px;font:inherit;font-size:13px}
.loop-editor textarea{font-family:var(--mono);font-size:12.5px;line-height:1.45;resize:vertical}
.le-help{font-size:12px;color:var(--dim);line-height:1.45;background:var(--panel);
border:1px solid var(--line);border-radius:7px;padding:7px 10px}
.le-help code{font-family:var(--mono);font-size:11.5px;color:var(--blue)}
.le-verdict{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}
.le-prompt{gap:4px}
.le-editing{font-size:13px;color:var(--fg)}
.le-cancel{color:var(--dim);text-decoration:none;margin-left:8px;font-size:12px}
.le-cancel:hover{color:var(--blue);text-decoration:underline}
.chip{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;margin:1px 4px 1px 0;
background:rgba(154,167,181,.12);color:var(--dim)}
.chip.red{background:rgba(248,81,73,.16);color:var(--red)}
.chip.amber{background:rgba(227,179,65,.16);color:var(--amber)}
.chip.info{background:rgba(154,167,181,.12);color:var(--dim)}
.sugs{margin:0 14px 12px;display:flex;flex-direction:column;gap:7px}
.sug{display:flex;gap:9px;align-items:flex-start;padding:8px 11px;border-radius:8px;
font-size:13px;line-height:1.4;background:var(--panel);border:1px solid var(--line);
border-left:3px solid var(--line)}
.sug.red{border-left-color:var(--red)}
.sug.amber{border-left-color:var(--amber)}
.sug.info{border-left-color:var(--green)}
.sug .badge{flex:0 0 auto;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;
padding:1px 6px;border-radius:4px;margin-top:1px;background:rgba(154,167,181,.14);color:var(--dim)}
.sug.red .badge{background:rgba(248,81,73,.16);color:var(--red)}
.sug.amber .badge{background:rgba(227,179,65,.16);color:var(--amber)}
.sug.info .badge{background:rgba(63,185,80,.14);color:var(--green)}
.sug-lead{font-weight:600;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.04em;
margin:2px 14px 6px}
.fp{color:var(--blue);font-size:11px;font-family:var(--mono);padding:0 4px}
input{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:4px 8px;font:inherit;font-size:13px}
input:focus{outline:none;border-color:var(--accent)}
select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:7px;
padding:5px 30px 5px 11px;font:inherit;font-size:13px;cursor:pointer;max-width:340px;
appearance:none;-webkit-appearance:none;-moz-appearance:none;
background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2 4l4 4 4-4' fill='none' stroke='%239aa7b5' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>");
background-repeat:no-repeat;background-position:right 10px center}
select:hover{border-color:#3b4756;background-color:var(--panel)}
select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(124,156,245,.18)}
select option{background:var(--panel);color:var(--fg)}
select option:disabled{color:var(--faint)}
.addwarn{margin:0 14px 10px;padding:8px 11px;border-radius:8px;font-size:12.5px;line-height:1.5;
background:rgba(227,179,65,.12);border:1px solid rgba(227,179,65,.35);color:var(--fg)}
.addwarn form{display:inline}.addwarn button{margin-left:4px}
form.addloop{margin:0;padding:12px 14px;display:flex;flex-wrap:wrap;gap:10px;align-items:end}
form.addloop label{display:flex;flex-direction:column;gap:3px;font-size:11px;
text-transform:uppercase;letter-spacing:.5px;color:var(--faint)}
details.addwrap{margin:0}details.addwrap>summary{list-style:none;cursor:pointer;
padding:10px 14px;color:var(--blue);font-size:13px;font-weight:500;user-select:none}
details.addwrap>summary::-webkit-details-marker{display:none}
details.addwrap>summary:hover{text-decoration:underline}
details.addwrap[open]>summary{color:var(--dim)}
/* pass #3 polish: motion, depth, focus, scrollbars */
a,button,input,.pill,.card,td.cell,tbody tr{transition:background-color .13s ease,
border-color .13s ease,color .13s ease,opacity .13s ease,box-shadow .13s ease,transform .08s ease}
table{box-shadow:0 1px 3px rgba(0,0,0,.28)}
header{box-shadow:0 1px 0 rgba(0,0,0,.35)}
.card:hover{border-color:#38424f}
button:active{transform:translateY(1px)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:5px}
::selection{background:rgba(124,156,245,.32)}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#2f3a47;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#3b4756}
@media(max-width:760px){.wrap{padding:16px}header{padding:14px 16px}h1{font-size:16px}
section{overflow-x:auto}td,th{padding:8px 10px}}
"""


def _now():
    return dt.datetime.now(dt.timezone.utc)


def gh_account() -> str | None:
    """The GitHub login `gh` is authenticated as -- shown in the header so it's clear which
    account drives github_state and issue actions (repos outside it degrade silently)."""
    import subprocess
    try:
        p = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() or None if p.returncode == 0 else None


def _td(row, col: str) -> str:
    """One repository table cell from a sqlite row (or a dash when absent)."""
    if row is None or row[col] is None:
        return '<span class="muted">no data</span>'
    return html.escape(str(row[col]))


def _h2(title: str, desc: str, color: str | None = None) -> str:
    """A section header with a descriptive one-line subtitle, so each section says what it is
    without the operator consulting the help card at the bottom. ``color`` tints the accent bar
    and title so each section reads as its own zone at a glance."""
    style = f' style="--hh:{color}"' if color else ''
    return (f'<h2{style}><span class="htitle">{title}</span> '
            f'<span class="subhead">{html.escape(desc)}</span></h2>')


def _badge(n: int) -> str:
    """A count pill for a section header, tinted by that section's accent colour."""
    return f'<span class="count-badge">{n}</span>'


def _foldablize(html: str, summaries: dict | None = None, open_ids: set | None = None) -> str:
    """Turn every ``<section data-sec="id">…</section>`` into a collapsible <details>. The header
    — the ``<h2>`` plus an immediately-following count badge — becomes the clickable summary;
    everything else the body. ``summaries`` maps a section id to a small HTML digest shown on the
    right of the header *only while collapsed*, so a folded section still gives the operator a
    reason to open it. ``open_ids`` is the set of sections that default OPEN (they have a signal);
    the rest start folded to their peek — a saved per-browser choice overrides this. One uniform
    mechanism so the whole board folds the same way. Sections don't nest, so a non-greedy match is
    safe."""
    import re
    summaries = summaries or {}
    open_ids = open_ids if open_ids is not None else set()
    hpat = re.compile(r'<h2\b.*?</h2>\s*(?:<span class="count-badge"[^>]*>.*?</span>)?', re.S)

    def repl(m):
        sid, inner = m.group(1), m.group(2)
        hm = hpat.search(inner)
        if not hm:
            return m.group(0)
        head = hm.group(0)
        peek = summaries.get(sid, "")
        peek_html = f'<span class="sec-sum">{peek}</span>' if peek else ""
        op = " open" if sid in open_ids else ""
        body = inner[:hm.start()] + inner[hm.end():]
        return (f'<details class="sec" id="sec-{sid}"{op}><summary>{head}{peek_html}</summary>'
                f'<div class="sec-body">{body}</div></details>')

    return re.sub(r'<section data-sec="([a-z-]+)">(.*?)</section>', repl, html, flags=re.S)


def _confirm(msg: str) -> str:
    return f'onsubmit="return confirm(&quot;{html.escape(msg)}&quot;)"'


def _run_form(project: str, cadence: str, label: str = "▶ Run now", cls: str = "") -> str:
    # Dispatch enqueues a run; it fires when a session next opens (or on the cloud routine),
    # not literally "now" — say so in the confirm so the button doesn't over-promise.
    return (f'<form method="post" action="/dispatch" style="display:inline" '
            f'{_confirm(f"Dispatch {cadence} for {project}? It runs when a session next opens in that project (or on the cloud routine).")}>'
            f'<input type="hidden" name="project" value="{html.escape(project)}">'
            f'<input type="hidden" name="cadence" value="{html.escape(cadence)}">'
            f'<button{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{label}</button></form>')


def _close_form(esc_id, project: str = "", cadence: str = "", issue=None) -> str:
    tgt = f"{project} / {cadence}".strip(" /") or "this escalation"
    gh = f" and its GitHub issue #{issue}" if issue else ""
    return (f'<form method="post" action="/close" style="display:inline" '
            f'{_confirm(f"Close the {tgt} escalation{gh}?")}>'
            f'<input type="hidden" name="id" value="{esc_id}">'
            f'<button title="resolve {html.escape(tgt)}{html.escape(gh)}">Close</button></form>')


def _autorun_form(project: str, on: bool) -> str:
    nextv, label = ("false", "turn off (queue)") if on else ("true", "turn on auto-run")
    verb = "queue (hold for next session)" if on else "auto-run (fire unattended)"
    return (f'<form method="post" action="/autorun" style="display:inline" '
            f'{_confirm(f"Switch {project} to {verb}?")}>'
            f'<input type="hidden" name="project" value="{html.escape(project)}">'
            f'<input type="hidden" name="value" value="{nextv}">'
            f'<button>{label}</button></form>')


def _register_form(cwd: str) -> str:
    """Register one discovered directory as a project (repo inferred from its git origin)."""
    return (f'<form method="post" action="/register" style="display:inline" '
            f'{_confirm(f"Register {cwd} as a project (repo from its git origin)?")}>'
            f'<input type="hidden" name="path" value="{html.escape(cwd)}">'
            f'<button title="add to registry.yaml; repo inferred from git origin">register</button>'
            f'</form>')


def _loop_editor(data=None) -> str:
    """The in-dashboard loop author/editor — create a new loop or edit an existing one entirely in
    the browser (no host editor). ``data`` (from loops.editable) pre-fills the edit case."""
    new = data is None
    d = data or {"name": "", "title": "", "metrics": [], "green": "", "amber": "", "prompt": ""}
    name = html.escape(d["name"])
    if new:
        name_field = ('<label>name<input name="name" placeholder="my-review" size="18" required '
                      'pattern="[a-z0-9][a-z0-9-]*" title="lowercase kebab-case, e.g. my-review"></label>')
    else:
        name_field = (f'<input type="hidden" name="name" value="{name}">'
                      f'<div class="le-editing">editing <b>{name}</b> '
                      f'<a class="le-cancel" href="/#sec-library">cancel</a></div>')
    return (
        f'<form method="post" action="/loop-author" class="loop-editor" id="loop-editor">'
        f'{name_field}'
        f'<label>title<input name="title" value="{html.escape(d["title"])}" '
        'placeholder="What it reviews" size="34"></label>'
        f'<label>metrics<input name="metrics" value="{html.escape(", ".join(d["metrics"]))}" '
        'placeholder="high_findings, coverage_pct" size="34" required></label>'
        '<div class="le-help">ⓘ <b>Metrics</b> are the numbers your review reports — one or more, '
        'comma-separated, short snake_case. The verdict is computed from them, and your prompt must '
        'output each one. <i>Examples:</i> <code>high_findings</code>, <code>coverage_pct</code>, '
        '<code>broken_links</code>.</div>'
        f'<div class="le-verdict"><label>green when<input name="green" value="{html.escape(d["green"])}" '
        'placeholder="high_findings == 0" size="22"></label>'
        f'<label>amber when<input name="amber" value="{html.escape(d["amber"])}" '
        'placeholder="high_findings &lt; 5" size="22"></label>'
        '<span class="le-help" style="margin:0">use the metric names above; red = anything else</span></div>'
        '<label class="le-prompt">instructions — what this loop should do'
        f'<textarea name="prompt" rows="8" placeholder="Describe the review a Claude session runs: '
        'what to inspect, what counts as a problem, and how to compute each metric. This becomes the '
        f'loop&#39;s prompt.">{html.escape(d["prompt"])}</textarea></label>'
        f'<div class="acts" style="margin:0"><button>{"Create loop" if new else "Save changes"}</button>'
        '<span class="na">Writes <code>loops/&lt;name&gt;.yaml</code> (metrics + verdict) and '
        '<code>prompts/&lt;name&gt;.md</code> (these instructions) — all editable here later, no '
        'external editor.</span></div></form>')


def _env_edit_form(path: Path) -> str:
    """A button that opens one .env file in the operator's editor. The dashboard binds
    127.0.0.1, so the server host is the operator's machine; the file opens there and its
    values never traverse HTTP (consistent with the section's values-never-here principle)."""
    return (f'<form method="post" action="/env-edit" style="display:inline">'
            f'<input type="hidden" name="path" value="{html.escape(str(path))}">'
            f'<button title="open {html.escape(str(path))} in your editor '
            f'(opens on this host; values stay off the wire)">✎ {html.escape(path.name)}</button>'
            f'</form>')


def _editor_command(path: str) -> list[str]:
    """Resolve the command to open a file in the operator's editor. FOREMAN_EDITOR wins (must
    be a GUI/detachable command, e.g. 'code' or 'open -t' — a terminal editor like vim can't
    run from the server with no TTY); else the OS default text editor."""
    override = os.environ.get("FOREMAN_EDITOR")
    if override:
        return shlex.split(override) + [path]
    if sys.platform == "darwin":
        return ["open", "-t", path]        # macOS default text editor
    return ["xdg-open", path]              # Linux default handler


def _open_env_in_editor(path: str) -> None:
    """Launch the editor detached; never blocks the request or needs a TTY."""
    subprocess.Popen(_editor_command(path),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def _schedule_form(project: str, loop: str, current: str | None, autorun: bool,
                   default_cron: str) -> str:
    """Frequency-preset picker for one (project, loop). A preset fires the loop unattended, so
    it is only offered when the project is on auto-run; in queue mode we show why it's off."""
    if not autorun:
        return ('<span class="na" title="a frequency preset fires the loop unattended — turn '
                'on auto-run for this project (Automation section, above) to schedule it">'
                'auto-run required</span>')
    hidden = (f'<input type="hidden" name="project" value="{html.escape(project)}">'
              f'<input type="hidden" name="loop" value="{html.escape(loop)}">')
    n = scheduler.interval_days(current or "")       # the loop's current "every N days", if any
    every_active = ' class="sched-on"' if n is not None else ''
    # primary control: "every [N] days" (the requested flexible cadence; default 1)
    every = (f'<form method="post" action="/loop-schedule" class="sched-every"{every_active}>{hidden}'
             f'every <input type="number" name="days" min="1" max="365" value="{n or 1}" '
             f'title="run this loop once every N days">'
             f'<button>set</button></form>')
    # secondary: the named alternatives + revert-to-default, auto-submitting
    choices = [("", f"or… (default: {default_cron})"), ("weekdays", "weekdays"),
               ("weekly", "weekly"), ("monthly", "monthly"), ("default", "← revert to default")]
    sel = current if (current in ("weekdays", "weekly", "monthly")) else ""
    opts = "".join(f'<option value="{v}"{" selected" if sel == v else ""}>{html.escape(lbl)}</option>'
                   for v, lbl in choices)
    named = (f'<form method="post" action="/loop-schedule" class="sched-named">{hidden}'
             f'<select name="preset" onchange="this.form.submit()">{opts}</select></form>')
    return f'<div class="sched">{every}{named}</div>'


# "tier" is the schema/receipt field; the UI calls it "where it runs" with plain-English values.
_TIER_LABEL = {"local": "this host", "cloud": "cloud", "session": "next session"}


def _tier_form(project: str, loop: str, current: str | None, default_tier: str | None) -> str:
    """Per-(project, loop) 'where it runs' picker (the cadence ``tier``). 'this host' runs it on
    this machine's scheduler (auto-run permitting); 'cloud' hands it to the cloud routine; 'next
    session' delivers it into the next Claude Code session; 'default' follows the cadence."""
    cur = current or "default"
    dt_lbl = f"default ({_TIER_LABEL.get(default_tier, default_tier)})" if default_tier else "default"
    choices = [("default", dt_lbl), ("local", "this host"),
               ("cloud", "cloud"), ("session", "next session")]
    opts = "".join(f'<option value="{v}"{" selected" if cur == v else ""}>{html.escape(lbl)}</option>'
                   for v, lbl in choices)
    return (f'<form method="post" action="/loop-tier" style="display:inline" '
            f'{_confirm(f"Change where {loop} runs for {project}?")}>'
            f'<input type="hidden" name="project" value="{html.escape(project)}">'
            f'<input type="hidden" name="loop" value="{html.escape(loop)}">'
            f'<select name="tier" onchange="this.form.submit()" title="where this '
            f'loop runs for {html.escape(project)}">{opts}</select></form>')


def _disable_form(project: str, loop: str) -> str:
    """Remove a loop from a project (inverse of enable). Destructive-ish, so confirm."""
    return (f'<form method="post" action="/loop-disable" style="display:inline" '
            f'{_confirm(f"Remove {loop} from {project}? (the loop definition is kept)")}>'
            f'<input type="hidden" name="project" value="{html.escape(project)}">'
            f'<input type="hidden" name="loop" value="{html.escape(loop)}">'
            f'<button title="remove this loop from {html.escape(project)}">✕ remove</button></form>')


def _window_form(project: str | None, window: tuple, label: str) -> str:
    """Time-of-day window editor for one project (or the fleet default when project is None)."""
    start, span, minute = window
    scope = "" if project is None else project
    return (f'<form method="post" action="/autorun-window" style="display:inline-flex;gap:6px;align-items:center" '
            f'{_confirm(f"Set the auto-run window for {label}?")}>'
            f'<input type="hidden" name="project" value="{html.escape(scope)}">'
            f'<label class="dim">from <input name="start_hour" value="{start}" size="2" '
            'inputmode="numeric" style="width:38px">:'
            f'<input name="start_minute" value="{"" if minute is None else minute}" size="2" '
            'placeholder="any" title="exact minute; blank = spread across the window" style="width:44px"></label>'
            f'<label class="dim">for <input name="span_hours" value="{span}" size="2" '
            'inputmode="numeric" style="width:38px">h</label>'
            '<button>set</button></form>')


def _gi(row, col: str) -> int:
    """Int cell from a sqlite row, 0 when missing/None."""
    try:
        v = row[col]
    except (KeyError, IndexError, TypeError):
        return 0
    return int(v) if v is not None else 0


def _stat(label: str, value: str, level: str = "") -> str:
    cls = f" {level}" if level in ("green", "amber", "red") else ""
    return (f'<div class="stat{cls}"><span class="v">{value}</span>'
            f'<span class="k">{html.escape(label)}</span></div>')


def _sparkline(verdicts: list) -> str:
    """A row of small bars for a loop's recent verdicts (oldest→newest) — flakiness/trend at a
    glance. Empty string when there's no history yet."""
    if not verdicts:
        return ""
    cls = {"green": "green", "amber": "amber", "red": "red"}
    bars = "".join(f'<span class="spark {cls.get(v, "stale")}"></span>' for v in verdicts)
    return (f'<span class="sparkline" title="last {len(verdicts)} runs (oldest → newest)">'
            f'{bars}</span>')


def _money(x: float) -> str:
    if not x:
        return '<td class="money zero">$0</td>'
    return f'<td class="money">${x:,.2f}</td>' if x < 1000 else f'<td class="money">${x/1000:,.1f}k</td>'


def _tokens(n: int) -> str:
    if not n:
        return '<span class="muted">0</span>'
    return f"{n/1e6:.1f}M" if n >= 1e6 else (f"{n/1e3:.0f}k" if n >= 1000 else str(n))


def _dispatch_launchable(reg: dict, project: str, host: str) -> bool:
    """A queued dispatch can be run headlessly *now* only if the project's worktree for this host
    exists on disk — the local scheduler spawns the run there (the same path the launchd agent
    uses). Off-host / no-worktree dispatches still just wait for the next session."""
    p = next((x for x in reg.get("projects", []) if x["slug"] == project), None)
    wt = (p or {}).get("worktree", {}).get(host)
    return bool(wt and Path(os.path.expanduser(wt)).is_dir())


def _queued_section(pending_decisions, reg: dict, host: str) -> str:
    """The operator-action queue: decisions waiting to apply (run a loop off-cycle, approve a
    push, apply a setting). Rendered high on the page — these are the things asking for a click."""
    out = [f'<section data-sec="queued">{_h2(f"Queued {_badge(len(pending_decisions))}", "operator actions waiting — run a loop off-cycle, approve a push, apply a setting", _C_MANUAL)}']
    if not pending_decisions:
        out.append('<div class="card na">Nothing waiting to apply. Dispatched runs and approvals '
                   'queue here until you apply them (or the next session picks them up).</div>')
    for _, dec in pending_decisions:
        did = dec["decision_id"]
        pl = dec.get("payload") or {}
        is_approval = dec["kind"] == "approve_push"
        is_dispatch = dec["kind"] == "dispatch_cadence"
        summary = {"dispatch_cadence": f"run {pl.get('cadence', '')}",
                   "note": pl.get("text", ""), "apply_setting": f"set {pl.get('path', '')}",
                   "answer_question": f"answer {pl.get('question_ref', '')}",
                   "set_pin": f"pin -> {pl.get('marketplace_pin', '')}",
                   "close_escalation": f"close {pl.get('escalation', '')}",
                   "approve_push": (f"push {pl.get('branch', '')} → PR onto "
                                    f"{pl.get('base', '')} ({pl.get('ahead', '?')} commits)")
                   }.get(dec["kind"], "")
        headless = D.can_apply_headless(dec)
        # a dispatch whose project worktree is on THIS host can be launched now (claude -p)
        can_run = is_dispatch and _dispatch_launchable(reg, dec["project"], host)
        proj = html.escape(dec["project"])
        acts = []
        if can_run:
            cad_name, proj_name = pl.get("cadence", ""), dec["project"]
            run_confirm = _confirm(f"Run {cad_name} for {proj_name} now? This spawns a headless "
                                   "Claude run (tokens/cost) and writes a receipt.")
            acts.append(
                f'<form method="post" action="/dispatch-run" style="display:inline" {run_confirm}>'
                f'<input type="hidden" name="id" value="{did}">'
                '<button title="run it headless on this host now">▶ Run now</button></form>')
        if headless:
            apply_label = "Approve &amp; push" if is_approval else "Apply now"
            apply_confirm = _confirm(
                f"Push {pl.get('branch', '')} and open a PR for {dec['project']}?" if is_approval
                else f"Apply this {dec['kind']} for {dec['project']} now?")
            acts.append(f'<form method="post" action="/decision-apply" style="display:inline" '
                        f'{apply_confirm}>'
                        f'<input type="hidden" name="id" value="{did}"><button>{apply_label}</button></form>')
        acts.append(f'<form method="post" action="/decision-dismiss" style="display:inline" '
                    f'{_confirm("Dismiss this queued decision?")}>'
                    f'<input type="hidden" name="id" value="{did}">'
                    f'<button title="cancel without applying">Dismiss</button></form>')
        if is_dispatch:
            # a dispatch's default fate is a status shown as a caption under the item (not an
            # inline chip beside the buttons): it fires when the project is next opened. "Run now"
            # above is the override.
            head = (f'<b>{proj}</b> <span class="dim">·</span> run '
                    f'<b>{html.escape(pl.get("cadence", ""))}</b>')
            sub = ('<div class="dim" title="Foreman delivers this to the next Claude Code session '
                   'opened in this project — nothing is spawned until then">Otherwise runs the next '
                   f'time you open {proj} in Claude Code.</div>')
        else:
            waits = ("pushes the branch and opens a PR when you approve" if is_approval else
                     "edits this project's files now — no window needed"
                     if headless and dec["requires_session"] else
                     "applies on the supervisor drain" if headless else
                     f"runs when you next open {proj} in Claude Code")
            head = (f'<b>{proj}</b> · {html.escape(dec["kind"])} '
                    f'<span class="dim">— {html.escape(summary)}</span>')
            sub = f'<div class="dim">{waits}</div>'
        out.append(f'<div class="card">{head}{sub}'
                   f'<div class="acts">{" ".join(acts)}</div></div>')
    out.append('</section>')
    return "".join(out)


def _repo_stats(g, h) -> str:
    """Always-on vital-sign tiles per project (not just threshold chips): sync, working tree,
    PRs, CI, issues, alerts, plus any non-zero hygiene counters. Complements repo_health.signals,
    which only speaks up when a threshold trips."""
    tiles = []
    if g is not None:
        ahead, behind = _gi(g, "ahead"), _gi(g, "behind")
        tiles.append(_stat("sync ↑↓", f"{ahead}/{behind}", "amber" if behind else ""))
        dirty = _gi(g, "dirty_files")
        tiles.append(_stat("dirty files", str(dirty),
                            "amber" if dirty > repo_health.DIRTY_AMBER else
                            ("" if dirty == 0 else "amber")))
        wt, orphan = _gi(g, "worktrees"), _gi(g, "orphan_worktrees")
        if wt or orphan:
            tiles.append(_stat("worktrees", f"{wt}" + (f" ({orphan}✗)" if orphan else ""),
                               "red" if orphan else ""))
        for col, lbl in (("stale_branches", "stale br"), ("unmerged_agent_branches", "agent br")):
            if _gi(g, col):
                tiles.append(_stat(lbl, str(_gi(g, col)), "amber"))
        try:
            blob = float(g["largest_blob_mb"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            blob = 0.0
        if blob >= 1:
            tiles.append(_stat("largest blob", f"{blob:.0f}MB",
                               "red" if blob >= repo_health.BLOB_RED_MB else "amber"))
        if _gi(g, "force_pushes_7d"):
            tiles.append(_stat("force push 7d", str(_gi(g, "force_pushes_7d")), "red"))
    if h is not None:
        prs, agent = _gi(h, "open_prs"), _gi(h, "agent_prs")
        tiles.append(_stat("open PRs", f"{prs}" + (f" ({agent}🤖)" if agent else ""), ""))
        old = _gi(h, "oldest_pr_days")
        if old:
            tiles.append(_stat("oldest PR", f"{old}d",
                               "amber" if old > repo_health.OLD_PR_AMBER_DAYS else ""))
        checks = _gi(h, "failing_checks")
        tiles.append(_stat("CI checks", "✓" if checks == 0 else f"{checks}✗",
                            "green" if checks == 0 else "red"))
        alerts = _gi(h, "security_alerts")
        tiles.append(_stat("sec alerts", str(alerts), "red" if alerts else "green"))
        if _gi(h, "dependabot_open"):
            tiles.append(_stat("dependabot", str(_gi(h, "dependabot_open")), "amber"))
        tiles.append(_stat("open issues", str(_gi(h, "issues_open"))))
        if _gi(h, "actions_minutes_month"):
            tiles.append(_stat("CI min/mo", str(_gi(h, "actions_minutes_month"))))
    if not tiles:
        return '<div class="stats"><span class="na">no git/GitHub data collected yet</span></div>'
    return f'<div class="stats">{"".join(tiles)}</div>'


def _loop_facts(cad: dict | None) -> str:
    """Compact read-only chips describing what a loop may change (writes), its per-run caps
    (budget / timeout), and whether it escalates — the safety envelope, surfaced in the library."""
    if not cad:
        return ''
    chips = []
    w = cad.get("writes") or {}
    if w:
        parts = ["opens PR" if w.get("open_pr") else "no PR"]
        if w.get("max_files_changed") is not None:
            mx = w["max_files_changed"]
            parts.append(f"≤{mx} file{'s' if mx != 1 else ''}")
        if w.get("branch"):
            parts.append(f"→ {w['branch']}")
        chips.append(f'<span class="chip info" title="what this loop may change">writes: '
                     f'{html.escape(", ".join(parts))}</span>')
    else:
        chips.append('<span class="chip info" title="no writes block — measures only, changes '
                     'nothing">read-only</span>')
    b = cad.get("budget") or {}
    caps = []
    if cad.get("timeout_minutes") is not None:
        caps.append(f"⏱ {cad['timeout_minutes']}m")
    if b.get("max_cost_usd") is not None:
        caps.append(f"≤${b['max_cost_usd']}")
    if b.get("max_tokens") is not None:
        caps.append(f"≤{b['max_tokens']} tok")
    if b.get("wall_clock_minutes") is not None:
        caps.append(f"≤{b['wall_clock_minutes']}m wall")
    if b.get("max_iterations") is not None:
        caps.append(f"≤{b['max_iterations']} iter")
    if caps:
        chips.append(f'<span class="chip info" title="per-run stop conditions (budget) + timeout">'
                     f'caps: {html.escape(" · ".join(caps))}</span>')
    if cad.get("escalate_when"):
        chips.append(f'<span class="chip amber" title="opens a GitHub issue on this verdict">'
                     f'escalates on {html.escape(str(cad["escalate_when"]))}</span>')
    return '<div style="margin-top:5px">' + " ".join(chips) + '</div>'


def _pill(verdict_or_label: str) -> str:
    label = verdict_or_label
    cls = "dash"
    for k in ("green", "amber", "red", "stale"):
        if verdict_or_label.startswith(k):
            cls = k
            break
    if verdict_or_label == "-":
        return '<span class="muted">—</span>'
    return f'<span class="pill {cls}">{html.escape(label)}</span>'


def _gather(foreman_dir: Path, state_dir: Path, index_path: str | None):
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    cadences = brief._cadence_meta(foreman_dir)
    groups = brief.load_receipts(state_dir)
    now = _now()
    projects = [p["slug"] for p in registry.get("projects", [])]
    pcad = {p["slug"]: list(p.get("cadences", [])) for p in registry.get("projects", [])}
    columns: list[str] = []
    for slug in projects:
        for c in pcad[slug]:
            if c not in columns:
                columns.append(c)
    conn = db.connect(index_path) if index_path and Path(index_path).exists() else None
    # Hosted/read-only fallback: the state branch (receipts) isn't deployed to the hosted board,
    # only the committed index.db — so with no receipts every verdict would read "stale". The
    # `run` table mirrors receipts, so synthesise a history from it for any pair the receipt store
    # doesn't cover. Local (receipts present) skips this entirely, so behaviour there is unchanged.
    if conn is not None:
        for slug in projects:
            for c in pcad[slug]:
                if groups.get((slug, c)):
                    continue
                rows = conn.execute(
                    "SELECT ended, verdict, status FROM run WHERE project=? AND cadence=? "
                    "AND status NOT IN ('locked','skipped') AND ended IS NOT NULL ORDER BY ended",
                    (slug, c)).fetchall()
                if rows:
                    groups[(slug, c)] = [{"project": slug, "cadence": c, "ended": r["ended"],
                                          "verdict": r["verdict"], "status": r["status"]}
                                         for r in rows]
    analysis = {}
    for slug in projects:
        for c in pcad[slug]:
            analysis[(slug, c)] = brief.analyze_pair(groups.get((slug, c), []), cadences.get(c), now)
    from collectors import keyscan
    keyshare = keyscan.load_lookup(foreman_dir)   # (project,key) -> [other projects] sharing a value
    return dict(registry=registry, projects=projects, pcad=pcad, columns=columns,
                analysis=analysis, groups=groups, now=now, conn=conn, cadences=cadences,
                foreman_dir=foreman_dir, state_dir=state_dir, keyshare=keyshare)


def _repo_active(cells: list, slug: str, pending_projects: set) -> bool:
    """A per-repo accordion auto-opens on a real signal: a failing/aging loop, a genuinely
    overdue one (stale WITH a prior run, not merely never-run), or a queued decision."""
    return slug in pending_projects or any(
        c.get("effective") in ("red", "amber")
        or (c.get("effective") == "stale" and c.get("latest")) for c in cells)


def _status_dots(loops_for: list, cells: list) -> str:
    """A row of colored status dots (one per loop) for an accordion summary -- keeps the signal
    visible while collapsed."""
    return "".join(f'<span class="dot {c.get("effective") or ""}" '
                   f'title="{html.escape(lp)}: {c.get("effective") or "—"}"></span>'
                   for lp, c in zip(loops_for, cells))


def _repo_acc(acc: str, slug: str, active: bool, dots: str, count_label: str) -> str:
    """Open a per-repo <details> accordion with a status-summary header. ``acc`` namespaces the
    localStorage key so different sections remember the same repo independently."""
    return (f'<details class="repo-acc" data-acc="{acc}" data-repo="{html.escape(slug)}"'
            f'{" open" if active else ""}><summary><span class="repo">{html.escape(slug)}</span>'
            f'<span class="sumdots">{dots}</span><span class="count">{count_label}</span></summary>')


# Per-section accent colours, so each zone of the page reads as its own thing at a glance.
_C_NEEDS = "#f85149"     # red     — failing / overdue
_C_MANUAL = "#f0883e"    # orange  — queued operator actions awaiting a click
_C_GRID = "#58a6ff"      # blue    — the Loops panel
_C_REPO = "#a371f7"      # purple  — git/GitHub state
_C_COST = "#2ea043"      # green   — spend / usage
_C_LIB = "#39c5cf"       # teal    — catalogue
_C_API = "#e3b341"       # gold    — secrets / API keys
_C_OPS = "#8b949e"       # grey    — plumbing (drift, queued, discovered)


def _rel_delta(delta: dt.timedelta) -> str:
    """A compact 'in 2d 3h' / 'in 12m' style label for a positive timedelta."""
    secs = int(delta.total_seconds())
    if secs < 60:
        return "now"
    mins = secs // 60
    if mins < 60:
        return f"in {mins}m"
    hours = mins // 60
    if hours < 24:
        return f"in {hours}h {mins % 60}m" if mins % 60 else f"in {hours}h"
    days = hours // 24
    return f"in {days}d {hours % 24}h" if hours % 24 else f"in {days}d"


_ACT_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _ago(iso: str, now: dt.datetime) -> str:
    """Compact 'just now / 12m ago / 3h ago / 2d ago' from an ISO-Z timestamp. Tolerant of a
    fractional-seconds suffix (transcript timestamps carry milliseconds) and a 'Z' or offset."""
    try:
        t = dt.datetime.strptime(iso, _ACT_ISO).replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        try:
            t = dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            return iso
    secs = max(0, int((now - t).total_seconds()))
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _recent_activity(conn, pending_decisions, now: dt.datetime, *, analysis=None,
                     days: int = 7, limit: int = 30) -> list[dict]:
    """A merged, newest-first timeline of fleet events in the last ``days`` — completed runs,
    escalations opened/cleared, and queued decisions — from the index + decision queue (all
    already-timestamped; no transcript reads, per invariant 4). Non-green events carry a
    ``detail`` (the escalation summary, or the loop's current reason/next_action from analysis)
    so you can see *why* something went sideways without leaving the feed. Each event: ts, lvl,
    icon, text, and optionally detail."""
    if conn is None:
        return []
    analysis = analysis or {}
    since = (now - dt.timedelta(days=days)).strftime(_ACT_ISO)
    ev: list[dict] = []
    for r in conn.execute(
            "SELECT project, cadence, verdict, status, ended FROM run "
            "WHERE ended >= ? AND status NOT IN ('locked','skipped') "
            "ORDER BY ended DESC LIMIT 200", (since,)):
        v = r["verdict"] or r["status"]
        lvl = v if v in ("green", "amber", "red") else (
            "red" if r["status"] in ("failed", "timeout") else "stale")
        e = {"ts": r["ended"], "lvl": lvl, "icon": "▷",
             "text": f"{r['project']} / {r['cadence']} ran {v}"}
        if lvl in ("red", "amber", "stale"):
            a = analysis.get((r["project"], r["cadence"])) or {}
            detail = (a.get("latest") or {}).get("next_action") or a.get("reason")
            if detail:
                e["detail"] = detail
        ev.append(e)
    for r in conn.execute("SELECT project, cadence, opened, resolved, summary FROM escalation"):
        if r["opened"] and r["opened"] >= since:
            e = {"ts": r["opened"], "lvl": "red", "icon": "▲",
                 "text": f"{r['project']} / {r['cadence']} escalated"}
            if r["summary"]:
                e["detail"] = r["summary"]
            ev.append(e)
        if r["resolved"] and r["resolved"] >= since:
            ev.append({"ts": r["resolved"], "lvl": "green", "icon": "✓",
                       "text": f"{r['project']} / {r['cadence']} cleared"})
    _kind_phrase = {"dispatch_cadence": "queued a run", "approve_push": "queued a push to approve",
                    "apply_setting": "queued a setting change", "note": "left a note",
                    "answer_question": "queued a question", "set_pin": "queued a pin change",
                    "close_escalation": "queued an escalation to close"}
    for _, dec in pending_decisions:
        created = dec.get("created", "")
        if created >= since:
            phrase = _kind_phrase.get(dec["kind"], "queued an action")
            cad = (dec.get("payload") or {}).get("cadence")
            text = f"{dec['project']}: {phrase}" + (f" ({cad})" if cad and dec["kind"] == "dispatch_cadence" else "")
            ev.append({"ts": created, "lvl": "", "icon": "⁝", "text": text})
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev[:limit]


def _next_fire_label(cron_expr: str, now_local: dt.datetime) -> str:
    """'Thu 07:31 · in 2d' for the next occurrence of ``cron_expr`` (interpreted local, per repo
    convention that Claude Code reads cron in local time), or '—' if none within the horizon."""
    try:
        nxt = scheduler.next_fire(cron_expr, now_local)     # handles cron and 'every N days'
    except (ValueError, AttributeError):
        nxt = None
    if nxt is None:
        return '<span class="muted">—</span>'
    rel = _rel_delta(nxt - now_local)
    return (f'<span class="next-fire">{nxt:%a %H:%M}'
            f' <span class="rel">· {html.escape(rel)}</span></span>')


def _is_auto_scheduled(reg: dict, cad_meta: dict, project: str, loop: str) -> bool:
    """True if (project, loop) fires unattended: the project is on auto-run AND the loop's
    effective tier is local/session (cloud tier waits on the cloud routine, not this host)."""
    if not scheduler.autorun_for(reg, project):
        return False
    tier = scheduler.effective_tier(reg, project, loop, (cad_meta.get(loop) or {}).get("tier"))
    return tier in ("local", "session")


_ERR_LABEL = {
    "dispatch": "Dispatch failed — the run was not queued.",
    "close": "Close failed — the escalation (and its GitHub issue) was not closed.",
    "autorun": "Auto-run toggle failed — the setting was not changed.",
    "loop-schedule": "Schedule change failed — the frequency was not set (a preset needs auto-run).",
    "loop-tier": "Change failed — where this loop runs was not updated.",
    "loop-disable": "Remove failed — the loop was not removed from that project.",
    "autorun-window": "Window change failed — the auto-run time was not set.",
    "env-edit": "Couldn't open that .env — not an editable file on this host.",
    "decision-apply": "Apply failed — the queued decision was not applied.",
    "decision-dismiss": "Dismiss failed — the decision is still queued.",
    "dispatch-run": "Run failed — the loop was not launched (no on-host worktree, or it's locked).",
    "register": "Register failed — not added (already registered, or no git origin).",
    "register-scan": "Scan found nothing new to register under that folder.",
    "loop-enable": "Enable failed — the loop was not enabled for that project.",
    "loop-new": "Add loop failed — check the name is kebab-case and not already taken.",
    "loop-author": "Couldn't save the loop — check the name is lowercase kebab-case and you gave at least one metric.",
    "loop-edit": "Couldn't open that loop file — not an editable cadence/loop on this host.",
}


def _legend_strip() -> str:
    """A dismissible 'how to read this board' key: the colour language + the few house terms a
    first-time operator needs. Native <details> so it works without JS (and in the read-only
    hosted view); a tiny script below remembers the collapsed state per browser."""
    colours = (
        '<span class="key"><b>Colours:</b>'
        '<span><span class="dot green"></span> healthy</span>'
        '<span><span class="dot amber"></span> needs attention soon</span>'
        '<span><span class="dot red"></span> act now</span></span>')
    terms = [
        ("Loop", "a recurring AI review Foreman runs for a project (docs, security, UX…)."),
        ("Verdict", "the result of each run — green, amber, or red."),
        ("Where it runs", "on this host, in the cloud, or the next session you open there."),
        ("Queued", "an action waiting for your click — run a loop now, or approve a push."),
        ("Repo status", "each project’s git/GitHub health, led by a suggested next action."),
        ("Drift", "where a project’s real Claude Code config differs from what’s declared."),
    ]
    dls = "".join(f'<dl><dt>{t}</dt><dd>{html.escape(d)}</dd></dl>' for t, d in terms)
    return ('<details class="legend-strip" id="legend" open><summary>How to read this board '
            '<span class="hint">— colour key &amp; terms for a first look</span></summary>'
            f'<div class="legend-body">{colours}{dls}</div></details>')


def _recent_row_ago(r, now) -> str:
    """'ago' for a roster row from either an ISO ``ts`` (store) or epoch ``mtime`` (live read)."""
    iso = r.get("ts")
    if not iso and r.get("mtime"):
        try:
            iso = dt.datetime.fromtimestamp(r["mtime"], tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            iso = None
    return _ago(iso, now) if iso else ""


def _recent_section(roster: list, now, timelines: dict | None = None, *,
                    week: list | None = None, search_q: str = "",
                    results: list | None = None) -> tuple[str, str]:
    """The "Recently worked on" section body + collapsed peek. With the local prompt store it shows
    a search box over your prompt history, a "this week" per-project tally, and one row per project
    (expandable to that project's timeline). When ``search_q`` is set, the matching prompts replace
    the roster. Local-only (the caller gates it out of the read-only hosted view)."""
    if not roster and not search_q:
        return "", ""
    timelines = timelines or {}
    has_store = bool(timelines or week or results is not None)
    inner = []

    if has_store:      # search box (GET so it survives the 30s meta-refresh; clear = link to /)
        inner.append(
            f'<form method="get" action="/" class="pq"><input name="pq" value="{html.escape(search_q)}" '
            f'placeholder="search your prompt history…" autocomplete="off"><button>search</button>'
            + ('<a class="pq-clear" href="/">clear</a>' if search_q else '') + '</form>')

    if search_q:
        res = results or []
        if not res:
            inner.append(f'<div class="na">No prompts match “{html.escape(search_q)}”.</div>')
        else:
            items = "".join(
                f'<li><span class="rp-proj"><b>{html.escape(r["slug"])}</b></span>'
                f'<span class="rp-prompt">{html.escape(r["text"])}</span>'
                f'<span class="rp-when">{_ago(r["ts"], now)}</span></li>' for r in res)
            inner.append(f'<div class="dim" style="margin:2px 0 6px"><span class="n">{len(res)}</span> '
                         f'match{"es" if len(res) != 1 else ""} for “{html.escape(search_q)}”</div>'
                         f'<ol class="results">{items}</ol>')
    else:
        if week:
            wk = " · ".join(f'<b>{html.escape(w["slug"])}</b>&nbsp;{w["count"]}' for w in week[:8])
            inner.append(f'<div class="week"><span class="dim">this week:</span> {wk}</div>')
        rows = []
        for r in roster:
            slug = html.escape(r["slug"])
            head = (f'<span class="rp-proj"><b>{slug}</b></span>'
                    f'<span class="rp-prompt">{html.escape(r["prompt"])}</span>'
                    f'<span class="rp-when">{_recent_row_ago(r, now)}</span>')
            hist = timelines.get(r["slug"]) or []
            if len(hist) > 1:
                tl = "".join(f'<li><span class="tl-when">{_ago(h["ts"], now)}</span>'
                             f'{html.escape(h["text"])}</li>' for h in hist)
                rows.append(f'<details class="recent-row"><summary>{head}</summary>'
                            f'<ol class="timeline">{tl}</ol></details>')
            else:
                rows.append(f'<div class="recent-row flat">{head}</div>')
        inner.append(f'<div class="recent">{"".join(rows)}</div>')

    body = (f'<section data-sec="recent">{_h2("Recently worked on", "your recent prompts to Claude Code — search history, this-week tally, and a per-project timeline · this machine only, secrets masked", _C_LIB)}'
            + "".join(inner) + '</section>')
    peek = (f'<span><span class="n">{len(results or [])}</span> matches</span>' if search_q
            else f'<span><span class="n">{len(roster)}</span> projects</span>')
    return body, peek


def render(data, refresh: int = 30, gh_login: str | None = None, fingerprints: bool = True,
           error: str | None = None, read_only: bool = False, search: str | None = None,
           edit: str | None = None) -> str:
    reg, projects, analysis = data["registry"], data["projects"], data["analysis"]
    conn, now = data["conn"], data["now"]
    foreman_dir = Path(data["foreman_dir"])
    secsum: dict = {}       # per-section header digest, shown only while that section is collapsed
    open_ids: set = set()   # sections that default OPEN (have a signal); the rest start folded to
                            # their peek. A saved per-browser choice overrides this default.
    q = quota.headroom(conn) if conn else None
    low = quota.low(conn) if conn else False

    out = ['<!doctype html><html><head><meta charset="utf-8">']
    if refresh > 0:
        out.append(f'<meta http-equiv="refresh" content="{refresh}">')
    # A hosted read-only view (e.g. Vercel) has no local git/index to act on, so hide every
    # action control -- the status (tables + cards) is the whole value there.
    ro_css = "form,.acts{display:none!important}" if read_only else ""
    out.append(f'<title>Foreman</title><style>{CSS}{ro_css}</style></head>'
               f'<body data-now="{now:%Y-%m-%dT%H:%M:%SZ}">')
    # header meta: quota (green/amber), gh identity, fleet size, clock, refresh, help
    qcls = "warn" if (q is not None and q < 15) else "live"
    qs = f'<span class="{qcls}">quota {int(q)}%</span> · ' if q is not None else ""
    ghs = f'gh: {html.escape(gh_login)} · ' if gh_login else '<span class="warn">gh: not authed</span> · '
    refnote = f' · refresh {refresh}s' if refresh > 0 else ''
    tagline = ("supervisor for your Claude Code projects — it <b>schedules</b> the recurring "
               "reviews, <b>collects a verdict</b> from every run, and surfaces <b>what needs you</b>")
    out.append('<header><div class="brand"><h1>FOREMAN</h1>'
               f'<div class="tagline">{tagline}</div></div>'
               f'<div class="meta">{ghs}{qs}<b>{len(projects)}</b> projects · '
               f'{now:%a %d %b %H:%M}Z{refnote} · <a href="/help">help</a></div></header>'
               f'<div class="wrap">')
    if read_only:
        out.append('<div class="banner">read-only hosted view — manage from the local '
                   'dashboard (<span class="mono">python -m collectors.web</span>)</div>')
    if error:
        msg = _ERR_LABEL.get(error, f"{error}: action failed.")
        out.append(f'<div class="banner err">{html.escape(msg)} '
                   f'<span class="dim">(see the dashboard server log for details)</span></div>')
    if low:
        out.append('<div class="banner">quota low: non-red cadences deferred to the next window</div>')

    # ORIENTATION LEGEND -- the whole board speaks in colour + a few house terms; a first-time
    # operator has no key for either. This dismissible strip decodes the green/amber/red language
    # and the handful of words a newcomer needs, so the page is legible on sight (not hover-only).
    out.append(_legend_strip())

    # SINCE YOU WERE AWAY (#1) -- orient before acting: recent fleet events, with the ones newer
    # than your last visit marked "new" client-side (localStorage), so you can pick up where you
    # left off. pending_decisions is loaded here (also reused by Queued below).
    pending_decisions = D.load_pending(data["state_dir"])
    pending_projects = {dec["project"] for _, dec in pending_decisions}
    activity = _recent_activity(conn, pending_decisions, now, analysis=analysis, days=7)
    open_ids.add("away")            # the catch-up feed always opens — it's the orientation section
    if activity:
        _ar = sum(1 for e in activity if e.get("lvl") == "red")
        secsum["away"] = (f'<span><span class="n">{len(activity)}</span> events</span>'
                          + (f'<span class="red"><span class="dot red"></span>{_ar}</span>' if _ar else ""))
    else:
        secsum["away"] = '<span class="ok">quiet — nothing in 7 days</span>'
    out.append(f'<section data-sec="away">'
               f'{_h2("Since you were away", "recent fleet activity (last 7 days) — new since your last visit is marked", _C_REPO)}'
               '<span class="count-badge" id="newcount" style="margin-left:8px"></span>')
    if not activity:
        out.append('<div class="card na">No runs, escalations, or queued decisions in the last '
                   '7 days.</div>')
    else:
        # cap the feed height (~8 rows) and scroll the rest, so a busy week doesn't push the whole
        # board down — newest is on top.
        out.append('<div class="card actfeed">')
        for e in activity:
            detail = (f'<div class="act-detail">{html.escape(e["detail"])}</div>'
                      if e.get("detail") else "")
            out.append(f'<div class="activity {e["lvl"]}" data-ts="{html.escape(e["ts"])}">'
                       f'<span class="ic {e["lvl"]}">{e["icon"]}</span>'
                       f'<div class="act-body"><span>{html.escape(e["text"])}</span>{detail}</div>'
                       f'<span class="when">{_ago(e["ts"], now)}</span></div>')
        out.append('</div>')
    out.append('</section>')

    # NEEDS YOU -- readable, with an action per item
    open_esc, repo_of = {}, {p["slug"]: p.get("repo") for p in reg.get("projects", [])}
    if conn:
        for r in conn.execute("SELECT id, project, cadence, github_issue FROM escalation "
                              "WHERE resolved IS NULL"):
            open_esc[(r["project"], r["cadence"])] = {"id": r["id"], "issue": r["github_issue"]}

    needs = [(k, a) for k, a in analysis.items() if a["needs_you"]]
    # Needs-you = genuine to-dos (failing / overdue / aging). Never-run loops are not repeated
    # here; they surface in the Loops panel via their "never" verdict and manual/scheduled state.
    todo = [(k, a) for k, a in needs if not (a["effective"] == "stale" and a["latest"] is None)]

    def _prio(a):   # red first, then overdue, then amber; oldest within a tier first
        rank = 0 if a["effective"] == "red" else (1 if a["effective"] == "stale" else 2)
        age = brief._parse(a["latest"]["ended"]) if a["latest"] else now
        return (rank, age)
    todo.sort(key=lambda it: _prio(it[1]))

    def _card(proj, cad, a, *, with_actions=True):
        eff = a["effective"]
        if eff == "stale":
            what = ("Never run — run it to get a first verdict."
                    if a["latest"] is None else f"Overdue — {a['reason']}.")
        elif eff == "red":
            what = f"Failing. {a['reason']}." if a["reason"] else "Failing."
        else:
            what = f"{a['reason']}."
        na = (a["latest"] or {}).get("next_action", "") if a["latest"] else ""
        acts = [_run_form(proj, cad)]
        if with_actions:
            esc = open_esc.get((proj, cad))
            if esc and esc["issue"] and repo_of.get(proj):
                acts.append(f'<a href="https://github.com/{repo_of[proj]}/issues/{esc["issue"]}" '
                            f'target="_blank">issue #{esc["issue"]}</a>')
            if esc:
                acts.append(_close_form(esc["id"], proj, cad, esc.get("issue")))
        na_html = f'<div class="dim">→ {html.escape(na)}</div>' if na else ""
        return (f'<div class="card"><b>{html.escape(proj)} / {html.escape(cad)}</b> &nbsp; '
                f'{_pill(a["label"])}<div>{html.escape(what)}</div>{na_html}'
                f'<div class="acts">{" &nbsp; ".join(acts)}</div></div>')

    secsum["needs"] = (f'<span><span class="n">{len(todo)}</span> to act</span>' if todo
                       else '<span class="ok">✓ all clear</span>')
    if todo:
        open_ids.add("needs")
    out.append(f'<section data-sec="needs">{_h2(f"Needs you {_badge(len(todo))}", "red verdicts and ambers aged to red — what wants a decision now", _C_NEEDS)}')
    if not todo:
        out.append('<div class="card ok-empty">✓&nbsp; Nothing failing or overdue</div>')
    for (proj, cad), a in todo:
        out.append(_card(proj, cad, a))
    out.append('</section>')

    # Partition every enabled loop by HOW it runs: manual (needs a click) vs auto (fires on a
    # schedule). This is the operator's core question — "what must I launch, and what runs itself?"
    cad_meta = data.get("cadences", {})
    proj_tiers = {p["slug"]: (p.get("tiers") or {}) for p in reg.get("projects", [])}
    # loop -> human title, for fuller dropdown labels ("security-review — Security review")
    loop_title = {n: s.get("title", n) for n, s in loops.CATALOG.items()}
    for _n, _m in cad_meta.items():
        if _m and _m.get("title"):
            loop_title[_n] = _m["title"]

    def _opt(value: str, label: str, *, selected=False, disabled=False) -> str:
        attrs = (" selected" if selected else "") + (" disabled" if disabled else "")
        return f'<option value="{html.escape(value)}"{attrs}>{html.escape(label)}</option>'
    now_local = dt.datetime.now()                       # cron is interpreted local (repo convention)
    manual_by: dict = {}                                # slug -> [(loop, analysis)]
    scheduled: list = []                                # (slug, loop, analysis, cron, next_dt)
    for slug in projects:
        for loop in data["pcad"].get(slug, []):
            a = analysis[(slug, loop)]
            if _is_auto_scheduled(reg, cad_meta, slug, loop):
                default_cron = (cad_meta.get(loop) or {}).get("schedule", "")
                cron = scheduler.effective_schedule(reg, slug, loop, default_cron)
                try:
                    nxt = validate.Cron.parse(cron).next_fire(now_local)
                except (ValueError, AttributeError):
                    nxt = None
                scheduled.append((slug, loop, a, cron, nxt))
            else:
                manual_by.setdefault(slug, []).append((loop, a))
    manual_total = sum(len(v) for v in manual_by.values())

    # QUEUED (right after Needs you): operator actions shouldn't be buried. pending_decisions /
    # pending_projects were loaded up top (for the activity feed + accordion auto-open).
    secsum["queued"] = (f'<span><span class="n">{len(pending_decisions)}</span> waiting</span>'
                        if pending_decisions else '<span class="ok">nothing waiting</span>')
    if pending_decisions:
        open_ids.add("queued")
    out.append(_queued_section(pending_decisions, reg, os.environ.get("FOREMAN_HOST", "mbp")))

    # RECENTLY WORKED ON -- your own prompt history, to pick up your thread. Placed after the
    # fleet catch-up + triage (Since you were away / Needs you / Queued) since it's personal
    # context, not fleet urgency; it bridges into the manage zone below. LOCAL ONLY: prompt text is
    # never persisted to the committed index or shown on the hosted board (invariant 3).
    if not read_only:
        _host = os.environ.get("FOREMAN_HOST", "mbp")
        _cwds = {p["slug"]: (p.get("worktree") or {}).get(_host) for p in reg.get("projects", [])}
        _roster, _timelines, _week, _results = [], {}, [], None
        search_q = (search or "").strip()
        try:                                            # prefer the collected store (has history)
            from collectors import prompts
            if prompts.store_path().exists():
                _roster = prompts.latest_by_project(limit=8)
                for _r in _roster:
                    _timelines[_r["slug"]] = prompts.timeline(_r["slug"], limit=15)
                _cutoff = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                _week = prompts.counts_since(_cutoff)
                if search_q:
                    _results = prompts.search(search_q, limit=60)
        except Exception:
            _roster = []
        if not _roster and not search_q:                # fall back to a live bounded-tail read
            try:
                _roster = recent.recent_by_project(_cwds, limit=8)
            except Exception:
                _roster = []
        _recent_body, _recent_peek = _recent_section(_roster, now, _timelines, week=_week,
                                                      search_q=search_q, results=_results)
        if _recent_body:
            out.append(_recent_body)
            secsum["recent"] = _recent_peek
            open_ids.add("recent")

    # LOOPS -- one panel for the whole per-project loop surface: each project's loops with their
    # verdict, schedule (frequency preset + next fire, or "manual"), tier, plus auto-run/window
    # and run/remove/add controls. A summary line answers the cross-project questions (what fires
    # next, how many need a manual launch) that used to be their own sections.
    lr = loops.last_runs(conn) if conn else {}
    vhist = loops.recent_verdicts(conn) if conn else {}   # per-loop verdict trend (sparkline)
    try:
        catalog_names = sorted({e["loop"] for e in loops.catalog(foreman_dir)})
    except (OSError, KeyError, yaml.YAMLError):
        catalog_names = []
    scheduled.sort(key=lambda x: (x[4] or dt.datetime.max, x[0], x[1]))
    never = sum(1 for slug in projects for lp in data["pcad"].get(slug, [])
                if analysis[(slug, lp)]["latest"] is None)
    _loops_red = sum(1 for slug in projects for lp in data["pcad"].get(slug, [])
                     if analysis[(slug, lp)]["effective"] == "red")
    _loops_total = sum(len(data["pcad"].get(slug, [])) for slug in projects)
    _nextrun = ""
    if scheduled and scheduled[0][4]:
        _s0 = scheduled[0]
        _rel = _rel_delta(_s0[4] - now_local)
        _nextrun = (f'<span class="nextrun">next run: <b>{html.escape(_s0[1])}</b> '
                    f'<span class="dim">{html.escape(_rel)}</span></span>')
    secsum["loops"] = (f'<span><span class="n">{_loops_total}</span> loops</span>'
                       + _nextrun
                       + (f'<span class="red"><span class="dot red"></span>{_loops_red}</span>'
                          if _loops_red else ""))
    if _loops_red:
        open_ids.add("loops")
    out.append(f'<section data-sec="loops">{_h2("Loops", "every project’s loops in one place — verdict, schedule & next fire, where it runs, auto-run, and run / remove / add", _C_GRID)}')
    bits = [f'<span><span class="n">{len(scheduled)}</span> scheduled</span>']
    if scheduled and scheduled[0][4]:
        s0 = scheduled[0]
        bits.append(f'<span class="nextrun">next run: <span class="n">{html.escape(s0[1])}</span> '
                    f'on {html.escape(s0[0])} {_next_fire_label(s0[3], now_local)}</span>')
    bits.append(f'<span><span class="n">{manual_total}</span> manual</span>')
    if never:
        bits.append(f'<span><span class="n">{never}</span> never run</span>')
    fleet_w = scheduler.autorun_window(reg)
    fw_min = "spread" if fleet_w[2] is None else f"{fleet_w[2]:02d}"
    bits.append('<span class="muted">fleet window '
                f'<span class="mono">{fleet_w[0]:02d}:{fw_min}·{fleet_w[1]}h</span></span>'
                f'{_window_form(None, fleet_w, "the fleet default")}')
    out.append(f'<div class="sumline">{"".join(bits)}</div>')
    out.append('<div style="margin-top:14px">')
    for slug in projects:
        loops_for = data["pcad"].get(slug, [])
        if not loops_for:
            continue
        cells = [analysis[(slug, lp)] for lp in loops_for]
        active = _repo_active(cells, slug, pending_projects)
        ar = scheduler.autorun_for(reg, slug)
        n = len(loops_for)
        count = f'{n} loop{"s" if n != 1 else ""} · {"auto-run" if ar else "queue"}'
        out.append(_repo_acc("loops", slug, active, _status_dots(loops_for, cells), count))
        # per-project control strip: auto-run toggle + time-of-day window (folds in Automation).
        proj = next((p for p in reg.get("projects", []) if p["slug"] == slug), None)
        win = scheduler.autorun_window(reg, slug)
        has_own = isinstance((proj or {}).get("autorun_window"), dict)
        wlbl = ("inherits default" if not has_own else
                f'start {win[0]:02d}:{"00" if win[2] is None else f"{win[2]:02d}"}, lasts {win[1]}h')
        # "auto-run this project" is the concept; the pill states it, the toggle flips it.
        state = ('<span class="pill green">auto-run: on</span>' if ar
                 else '<span class="pill amber">auto-run: off (queued)</span>')
        out.append('<div class="acts" style="padding:10px 14px;border-bottom:1px solid var(--line)">'
                   f'{state}{_autorun_form(slug, ar)}'
                   f'<span class="dim" style="margin-left:auto" title="when auto-run loops may fire">'
                   f'auto-run window: <span class="mono">{wlbl}</span></span>'
                   f'{_window_form(slug, win, slug)}</div>')
        out.append('<table><tr><th>loop</th><th>last run</th><th>verdict</th>'
                   '<th>schedule</th><th>next fire</th><th>where it runs</th><th>actions</th></tr>')
        for lp in loops_for:
            a = analysis[(slug, lp)]
            info = lr.get((slug, lp))
            when = html.escape(info["ended"]) if info else '<span class="na">never</span>'
            default_cron = (cad_meta.get(lp) or {}).get("schedule", "—")
            current = scheduler.loop_preset(reg, slug, lp)
            eff_cron = scheduler.effective_schedule(reg, slug, lp, default_cron)
            auto = _is_auto_scheduled(reg, cad_meta, slug, lp)
            nextf = _next_fire_label(eff_cron, now_local) if auto else '<span class="muted">manual</span>'
            dtier = (cad_meta.get(lp) or {}).get("tier")
            out.append(f'<tr><td><b>{html.escape(lp)}</b></td><td>{when}</td>'
                       f'<td>{_pill(a["label"])}{_sparkline(vhist.get((slug, lp), []))}</td>'
                       f'<td>{_schedule_form(slug, lp, current, ar, default_cron)}</td>'
                       f'<td>{nextf}</td>'
                       f'<td>{_tier_form(slug, lp, proj_tiers.get(slug, {}).get(lp), dtier)}</td>'
                       f'<td><div class="acts" style="margin:0;gap:6px">'
                       f'{_run_form(slug, lp, label="▶ Run")}{_disable_form(slug, lp)}</div></td></tr>')
        out.append('</table>')
        available = [l for l in catalog_names if l not in loops_for]
        if available:
            loop_opts = "".join(_opt(l, f"{l} — {loop_title.get(l, l)}") for l in available)
            tier_opts = "".join(_opt(v, lbl) for v, lbl in
                                (("", "default — follow the loop"), ("local", "this host"),
                                 ("cloud", "cloud"), ("session", "next session")))
            freq_opts = "".join(_opt(v, lbl) for v, lbl in
                                (("", "default"), ("daily", "daily"), ("weekdays", "weekdays"),
                                 ("weekly", "weekly"), ("monthly", "monthly")))
            out.append(
                '<details class="addwrap"><summary>+ Add a loop to this project</summary>'
                f'<form class="addloop" method="post" action="/loop-enable" '
                f'{_confirm(f"Enable this loop for {slug}?")}>'
                f'<input type="hidden" name="project" value="{html.escape(slug)}">'
                f'<label>loop<select name="loop">{loop_opts}</select></label>'
                f'<label>where it runs<select name="tier">{tier_opts}</select></label>'
                f'<label>frequency<select name="preset">{freq_opts}</select></label>'
                '<button>Add loop</button></form>'
                # the one true failure the intuitive-ux review found: a first-timer picks a
                # frequency on a queue-mode project and the loop silently never fires. Flag it
                # inline with a one-click fix.
                + (f'<div class="addwarn">⚠ <b>{html.escape(slug)}</b> is in <b>queue</b> mode — a '
                   'frequency you pick here <b>won’t fire on a schedule</b> until you turn on '
                   'auto-run. '
                   f'<form method="post" action="/autorun" style="display:inline" '
                   f'{_confirm(f"Turn on auto-run for {slug}?")}>'
                   f'<input type="hidden" name="project" value="{html.escape(slug)}">'
                   '<input type="hidden" name="value" value="true">'
                   '<button>Turn on auto-run</button></form></div>' if not ar else '')
                + '<div class="na" style="padding:0 14px 12px"><b>Where it runs</b>: this host '
                '(local scheduler), the cloud routine, or your next Claude Code session — '
                '<i>default</i> follows the loop’s own setting. <b>Frequency</b> only fires when '
                'the project is on auto-run. Both can be changed later per row.</div></details>')
        out.append('</details>')
    out.append('</div></section>')

    # LOOPS CATALOG -- the manage surface: enable a loop for a project, add a library loop,
    # edit a loop's file. Loops are opt-in per project (each is a recurring run with real cost),
    # which is why they are not all on by default.
    try:
        cat = loops.catalog(foreman_dir)
    except Exception:
        cat = []
    if cat:
        secsum["library"] = f'<span><span class="n">{len(cat)}</span> loops available</span>'
    out.append(f'<section data-sec="library">{_h2("Loop library", "every review loop available — pick a project to add it to, or edit its definition", _C_LIB)}'
               '<div class="na">Opt-in per project: each enabled loop is a recurring run with '
               'real cost, and not every review fits every project — so they are not all on by '
               'default. Enable the ones a project needs; add your own with <b>New loop</b>.</div>'
               '<table><tr><th>loop</th><th>kind</th><th>enabled for</th>'
               '<th>enable for…</th><th>edit</th></tr>')
    for e in cat:
        loop = e["loop"]
        enabled = ", ".join(html.escape(s) for s in e["enabled_for"]) or '<span class="na">none</span>'
        enabled_set = set(e["enabled_for"])
        not_enabled = [s for s in projects if s not in enabled_set]
        if not not_enabled:
            enable_cell = '<span class="na">enabled everywhere</span>'
        else:
            # show the whole fleet; already-enabled projects are greyed + unselectable so the
            # dropdown is a complete picture, not a mystery short list.
            first = not_enabled[0]
            opts = "".join(
                _opt(s, s if s not in enabled_set else f"{s} · ✓ enabled",
                     selected=(s == first), disabled=(s in enabled_set))
                for s in projects)
            enable_cell = (f'<form method="post" action="/loop-enable" style="display:inline" '
                           f'{_confirm(f"Enable {loop} for this project?")}>'
                           f'<input type="hidden" name="loop" value="{html.escape(loop)}">'
                           f'<select name="project">{opts}</select> '
                           f'<button>enable</button></form>')
        # in-dashboard editor (no host editor): ?edit=<loop> re-renders with the editor pre-filled
        edit_cell = (f'<a class="editlink" href="/?edit={html.escape(loop)}#loop-editor" '
                     f'title="edit this loop in the dashboard">✎ edit</a>')
        facts = _loop_facts(cad_meta.get(loop))
        out.append(f'<tr><td><b>{html.escape(loop)}</b> '
                   f'<span class="dim">{html.escape(e.get("title", ""))}</span>{facts}</td>'
                   f'<td>{html.escape(e["kind"])}</td><td>{enabled}</td>'
                   f'<td>{enable_cell}</td><td>{edit_cell}</td></tr>')
    out.append('</table>')
    # In-dashboard author/editor. ?edit=<loop> pre-fills it from the loop's current definition;
    # otherwise it's a blank create form. Either way the loop is written entirely here — no host
    # editor — which is what the old "scaffold then open $EDITOR" flow was missing.
    _edit_data = None
    if edit:
        try:
            _edit_data = loops.editable(foreman_dir, edit)
        except Exception:
            _edit_data = None
    out.append(
        '<div class="card">'
        f'<div style="font-weight:600;font-size:14px;margin-bottom:9px">'
        f'{"✎ Edit loop" if _edit_data else "➕ Create a new loop"}</div>'
        + _loop_editor(_edit_data) + '</div></section>')
    if edit:
        open_ids.add("library")   # jump the operator straight to the pre-filled editor

    # DISCOVERED (scan session roots directly so it works before C1 has run) + register tools.
    # Sits here in the "manage what Foreman watches" zone (right after the loop catalogue), not
    # down in the observe/plumbing zone -- registering a project is a setup action, like enabling
    # a loop.
    try:
        disc = discover.discover_from_roots(reg)
    except Exception:
        disc = []
    secsum["discovered"] = (f'<span><span class="n">{len(disc)}</span> to register</span>' if disc
                            else '<span class="ok">all registered</span>')
    if disc:
        open_ids.add("discovered")
    out.append(f'<section data-sec="discovered">{_h2(f"Discovered {_badge(len(disc))} — unregistered", "local git repos Foreman has seen but you have not registered", _C_OPS)}')
    # Bulk: point at the folder your projects live in; register each git repo (matched to its
    # GitHub project by its origin remote), optionally filtered to one owner.
    out.append(
        '<form method="post" action="/register-scan" class="card" '
        f'{_confirm("Register every git repo under this folder (matched by its GitHub origin) as a project?")}>'
        'Register all git repos under '
        '<input name="base" value="~" size="22" placeholder="~/code"> '
        'owned by <input name="owner" size="12" placeholder="(any owner)"> '
        '<button>Scan &amp; register</button>'
        '<div class="na" style="margin-top:4px">matched to GitHub by each dir\'s '
        '<code>origin</code> remote; already-registered repos are skipped. Then run '
        '<code>collectors.validate</code> and commit <code>registry.yaml</code>.</div></form>')
    if disc:
        out.append('<table><tr><th>sessions</th><th>directory</th><th>git</th><th></th></tr>')
        for e in disc[:12]:
            git = "✓" if e.get("is_git") else "-"
            reg_btn = _register_form(e["cwd"]) if e.get("is_git") else \
                '<span class="na">no git remote</span>'
            out.append(f'<tr><td>{e["sessions"]}</td><td>{html.escape(e["cwd"])}</td>'
                       f'<td>{git}</td><td>{reg_btn}</td></tr>')
        out.append('</table>')
    out.append('</section>')

    # REPO state translated to actionable signals -- per-repo accordions (collapsed by default;
    # a repo with an amber/red signal auto-opens; open/closed remembered per browser). A summary
    # line up top rolls the fleet into clean / attention / issue / no-data counts.
    if conn:
        repo_rows = []          # (slug, lvl, has_data, sigs, branch_html, g, h)
        tally = {"green": 0, "amber": 0, "red": 0, "none": 0}
        fleet = {"open_prs": 0, "failing_checks": 0, "security_alerts": 0,
                 "dependabot_open": 0, "dirty": 0, "issues_open": 0}
        for slug in projects:
            g = conn.execute("SELECT * FROM git_state WHERE project=? ORDER BY taken DESC LIMIT 1",
                             (slug,)).fetchone()
            h = conn.execute("SELECT * FROM github_state WHERE project=? ORDER BY taken DESC LIMIT 1",
                             (slug,)).fetchone()
            sigs = repo_health.signals(g, h)
            sug = repo_health.suggestions(g, h)
            # derive the dot/roll-up from the same suggestions the body shows, not from signals —
            # otherwise the headline level could contradict the advice (intuitive-ux finding).
            lvl = repo_health.level_of_suggestions(sug)
            has_data = bool(g or h)
            branch = _td(g, "branch") if has_data else '<span class="muted">—</span>'
            tally[lvl if has_data and lvl in ("green", "amber", "red") else "none"] += 1
            if g is not None and _gi(g, "dirty_files") > 0:
                fleet["dirty"] += 1
            for col in ("open_prs", "failing_checks", "security_alerts", "dependabot_open", "issues_open"):
                fleet[col] += _gi(h, col)
            repo_rows.append((slug, lvl, has_data, sigs, sug, branch, g, h))
        _peek = []
        if tally["red"]:
            _peek.append(f'<span class="red"><span class="dot red"></span><span class="n">{tally["red"]}</span> issue</span>')
        if tally["amber"]:
            _peek.append(f'<span><span class="dot amber"></span><span class="n">{tally["amber"]}</span> attention</span>')
        if not _peek and tally["green"]:
            _peek.append('<span class="ok">✓ all clean</span>')
        secsum["repo"] = "".join(_peek)
        if tally["red"] or tally["amber"]:
            open_ids.add("repo")
        out.append(f'<section data-sec="repo">{_h2("Repo status", "git & GitHub health per project — vital-sign stats, roll-up, and what to act on", _C_REPO)}')
        parts = []
        if tally["green"]:
            parts.append(f'<span><span class="dot green"></span> <span class="n">{tally["green"]}</span> clean</span>')
        if tally["amber"]:
            parts.append(f'<span><span class="dot amber"></span> <span class="n">{tally["amber"]}</span> attention</span>')
        if tally["red"]:
            parts.append(f'<span><span class="dot red"></span> <span class="n">{tally["red"]}</span> issue</span>')
        if tally["none"]:
            parts.append(f'<span><span class="dot"></span> <span class="n">{tally["none"]}</span> no data</span>')
        # fleet roll-up: the cross-project totals that make this a dashboard, not just a list.
        for col, lbl in (("open_prs", "open PRs"), ("failing_checks", "failing checks"),
                         ("security_alerts", "security alerts"), ("dependabot_open", "dependabot"),
                         ("dirty", "dirty repos"), ("issues_open", "open issues")):
            if fleet[col]:
                lev = "red" if col in ("failing_checks", "security_alerts") else ""
                cl = f' class="{lev}"' if lev else ''
                parts.append(f'<span{cl}>·&nbsp; <span class="n">{fleet[col]}</span> {lbl}</span>')
        out.append(f'<div class="sumline">{"".join(parts)}</div>')
        out.append('<div style="margin-top:14px">')
        for slug, lvl, has_data, sigs, sug, branch, g, h in repo_rows:
            active = lvl in ("amber", "red") and has_data
            dot_cls = lvl if (has_data and lvl in ("green", "amber", "red")) else ""
            state = "clean" if (lvl == "green" and has_data) else (lvl if has_data else "no data")
            # the collapsed row shows the single most important thing to do (short label
            # before the em-dash), so the operator sees guidance without expanding.
            top = next((s for s in sug if s["level"] in ("red", "amber")), None)
            hint = ""
            if top:
                lead = html.escape(top["text"].split(" — ")[0])
                hint = f' · <span class="{top["level"]}">➜ {lead}</span>'
            out.append(_repo_acc("repo", slug, active, f'<span class="dot {dot_cls}"></span>',
                                 f'{state} · {branch}{hint}'))
            # lead the body with synthesized "what to do next", then the raw vital-sign tiles
            # and per-stat chips as secondary detail.
            out.append('<div class="sug-lead">Suggested next</div>')
            out.append('<div class="sugs">')
            for s in sug:
                out.append(f'<div class="sug {s["level"]}"><span class="badge">{s["level"]}</span>'
                           f'<span>{html.escape(s["text"])}</span></div>')
            out.append('</div>')
            # vital-sign tiles only. The per-stat chip row used to restate the same facts a third
            # time (after Suggested next + tiles) — dropped per the intuitive-ux review so a repo
            # card is action + vitals, not a wall that says everything thrice.
            out.append(_repo_stats(g, h))
            out.append('</details>')
        out.append('</div></section>')

    # SPEND & USAGE -- per-project cost/tokens/sessions from telemetry_day (already collected;
    # populated once the OTLP receiver runs and Claude Code exports foreman.project attributes).
    if conn:
        from collectors import telemetry
        spend = telemetry.spend_summary(conn, now=now, days=30, recent_days=7)
        if spend["projects"]:
            _t = spend["total"]
            secsum["spend"] = (f'<span><span class="n">${_t["cost"]:,.0f}</span> in 30d</span>'
                               f'<span><span class="n">{_t["sessions"]:,}</span> sessions</span>')
        else:
            secsum["spend"] = '<span class="muted">no telemetry yet</span>'
        out.append(f'<section data-sec="spend">{_h2("Spend &amp; usage", "cost, tokens and sessions per project over the last 30 days (from Claude Code telemetry)", _C_COST)}')
        if not spend["projects"]:
            out.append('<div class="card na">No telemetry ingested yet. This fills in once the '
                       'OTLP receiver runs (<span class="mono">telemetry install</span> / '
                       '<span class="mono">serve</span>) and Claude Code exports to it with '
                       '<span class="mono">foreman.project</span> resource attributes; '
                       '<span class="mono">collect --spool …</span> then drains it into the index.</div>')
        else:
            t = spend["total"]
            out.append(f'<div class="sumline"><span><span class="n">${t["cost"]:,.2f}</span> in 30d</span>'
                       f'<span><span class="n">{_tokens(t["tokens"])}</span> tokens</span>'
                       f'<span><span class="n">{t["sessions"]:,}</span> sessions</span>'
                       + "".join(f'<span class="muted">{html.escape(m["model"])} '
                                 f'${m["cost"]:,.0f}</span>' for m in spend["by_model"][:4])
                       + '</div>')
            peak = max((p["cost"] for p in spend["projects"]), default=0) or 1
            out.append('<table><tr><th>project</th><th>30d cost</th><th>7d</th>'
                       '<th>tokens</th><th>sessions</th><th>active days</th><th></th></tr>')
            for p in spend["projects"]:
                barw = int(round(100 * p["cost"] / peak)) if p["cost"] else 0
                bar = (f'<span class="bar" style="width:{barw}px"></span>' if barw else
                       '<span class="muted">—</span>')
                out.append(f'<tr><td class="proj">{html.escape(p["project"])}</td>'
                           f'{_money(p["cost"])}{_money(p["cost_recent"])}'
                           f'<td class="money">{_tokens(p["tokens"])}</td>'
                           f'<td class="money">{p["sessions"]:,}</td>'
                           f'<td class="money">{p["active_days"]}</td><td>{bar}</td></tr>')
            out.append('</table>')
        out.append('</section>')

    # DRIFT -- config resolver output: where what's ACTUALLY in effect diverges from what the
    # registry/cadences declare. An explainer up top so the section isn't cryptic when empty.
    if conn:
        lines = config_resolve.drift_lines(conn, reg)
        secsum["drift"] = (f'<span><span class="n">{len(lines)}</span> diverged</span>' if lines
                           else '<span class="ok">✓ in sync</span>')
        if lines:
            open_ids.add("drift")
        out.append(f'<section data-sec="drift">{_h2(f"Drift {_badge(len(lines))}", "declared vs effective config — pin mismatches, shadowed permissions, silent-skipped skills, budget breaches", _C_OPS)}')
        out.append('<div class="na" style="margin-top:10px">Foreman compares each project\'s '
                   '<b>declared</b> config (registry pins, where each loop runs, enabled skills/plugins) '
                   'against what a <span class="mono">claude -p</span> probe reports as '
                   '<b>actually in effect</b>. Anything here is a divergence to reconcile: a '
                   'marketplace-pin mismatch, a permission a lower layer silently overrides, a '
                   'skill that’s enabled but never invoked, or a run that breached its budget.</div>')
        if not lines:
            out.append('<div class="card ok-empty">✓&nbsp; Effective config matches what you declared</div>')
        for scope, msg in lines:
            out.append(f'<div class="card"><b>{html.escape(scope)}</b> &nbsp; {html.escape(msg)}</div>')
        out.append('</section>')

    # SECRETS -- managed keys (default/override) + keys found in .env that are NOT managed
    overrides = set()
    if conn:
        overrides = {r["name"] for r in conn.execute(
            "SELECT name FROM credential WHERE kind = 'env_key_override'")}
    # Pre-pass: digest every project's keys, then colour keys whose VALUE matches across
    # projects. Matching is by full-value sha256 (collision-proof); the last-5 fingerprint is
    # only for display. Digests are computed on demand and never stored (invariant 3).
    proj_wt = {p["slug"]: next(iter((p.get("worktree") or {}).values()), None)
               for p in reg.get("projects", [])}
    proj_dig = {slug: secrets.env_key_digests(wt) for slug, wt in proj_wt.items()
                if wt and fingerprints}
    groups: dict = {}            # (key, value-hash) -> set(projects)
    group_fp: dict = {}          # (key, value-hash) -> display fingerprint
    for slug, dig in proj_dig.items():
        for k, d in dig.items():
            groups.setdefault((k, d["hash"]), set()).add(slug)
            group_fp[(k, d["hash"])] = d["fp"]
    _PALETTE = ["#4aa3ff", "#2ecc71", "#e67e22", "#9b59b6", "#1abc9c", "#e84393", "#f39c12"]
    match_color = {kh: _PALETTE[i % len(_PALETTE)]
                   for i, kh in enumerate(sorted(kh for kh, ps in groups.items() if len(ps) >= 2))}

    def _fp(slug, k):
        d = proj_dig.get(slug, {}).get(k)
        if not d:
            return ''
        fp, kh = d["fp"], (k, d["hash"])
        color = match_color.get(kh)
        if color:
            others = ", ".join(sorted(groups[kh] - {slug}))
            return (f' <span class="fp" style="color:{color};border:1px solid {color}66" '
                    f'title="same value as: {html.escape(others)} (matched by full-value hash)">'
                    f'{html.escape(fp)}</span>')
        return f' <span class="fp" title="last 5 chars of the value in .env (unique here)">{html.escape(fp)}</span>'

    # Shared-key summary: keys whose full value is shared across >=2 projects. Rendered as a lead
    # alert INSIDE the "API keys & secrets" section below (co-located with the data it's about),
    # not a separate top-level section — it was redundant with the shared badges there.
    shared = sorted(((kh, sorted(ps)) for kh, ps in groups.items() if len(ps) >= 2),
                    key=lambda x: (-len(x[1]), x[0][0]))
    shared_cards = []
    for (k, _hash), ps in shared:
        color = match_color[(k, _hash)]
        shared_cards.append(f'<div class="card"><b>{html.escape(k)}</b> '
                            f'<span class="fp" style="color:{color};border:1px solid {color}66">'
                            f'{html.escape(group_fp[(k, _hash)])}</span> &nbsp; same value across '
                            f'<b>{len(ps)}</b>: {", ".join(html.escape(p) for p in ps)}</div>')

    matches = len(shared)
    hdr = f' <span class="dim">({matches} shared across projects)</span>' if matches else ''
    _n_decl = sum(1 for p in reg.get("projects", []) if p.get("env_keys"))
    secsum["secrets"] = (f'<span><span class="n">{_n_decl}</span> projects</span>'
                         + (f'<span class="amber"><span class="dot amber"></span>'
                            f'{matches} shared</span>' if matches else ""))
    if read_only:
        # Hosted board: no worktrees/.env to scan, so show what the committed registry declares
        # (names only, no values). This is the SPEC §19 declaration, not the live .env inventory.
        out.append(f'<section data-sec="secrets">{_h2("API keys &amp; secrets", "which API keys / secrets each project needs (from registry.yaml; names only, no values)", _C_API)}<table>'
                   '<tr><th>project</th><th>declared env keys</th></tr>')
        rows = 0
        for p in reg.get("projects", []):
            keys = p.get("env_keys") or []
            if not keys:
                continue
            rows += 1
            keyshare = data.get("keyshare") or {}
            parts = []
            for k in keys:
                others = keyshare.get((p["slug"], k))
                badge = (f' <span class="chip amber" title="same value in: '
                         f'{html.escape(", ".join(others))}">shared</span>') if others else ''
                parts.append(f'<span class="chip">{html.escape(k)}</span>{badge}')
            out.append(f'<tr><td><b>{html.escape(p["slug"])}</b></td><td>{" ".join(parts)}</td></tr>')
        if not rows:
            out.append('<tr><td colspan="2" class="na">no env keys declared in registry.yaml '
                       '(add <span class="mono">env_keys: [...]</span> to a project to list them here)</td></tr>')
        out.append('</table><div class="card na" style="margin-top:8px">Declared names only, from '
                   'the committed registry. Actual values and the live <span class="mono">.env</span> '
                   'inventory are visible only on the local dashboard on the host.</div></section>')
    else:
        out.append(f'<section data-sec="secrets">{_h2(f"API keys &amp; secrets{hdr}", "which API keys / secrets each project uses, and where a value is shared — collapsed by project", _C_API)}')
        n_decl = sum(1 for p in reg.get("projects", []) if p.get("env_keys"))
        sl = [f'<span><span class="n">{n_decl}</span> project{"s" if n_decl != 1 else ""} declare keys</span>']
        if matches:
            sl.append(f'<span><span class="n">{matches}</span> value{"s" if matches != 1 else ""} shared across projects</span>')
        sl.append('<span class="muted">values never traverse the dashboard — edit ▸ .env opens on this host</span>')
        out.append(f'<div class="sumline">{"".join(sl)}</div>')
        if shared_cards:
            out.append('<div class="sug-lead" style="color:var(--amber)">Shared values — one '
                       'secret reused across projects; rotate once, everywhere</div>')
            out.extend(shared_cards)
        out.append('<div style="margin-top:14px">')
        any_row = False
        for p in reg.get("projects", []):
            slug = p["slug"]
            declared = p.get("env_keys", []) or []
            wt = proj_wt.get(slug)
            unmanaged = secrets.unmanaged_env_keys(wt, declared) if wt else []
            if not declared and not unmanaged:
                continue
            any_row = True
            managed_cells = []
            for k in declared:
                if f"project:{slug}/{k}" in overrides:
                    scope, why = "project", f"a {slug}-specific value replaces the shared default"
                elif any(o.startswith("host:") and o.endswith("/" + k) for o in overrides):
                    scope, why = "host", "a host-specific value replaces the shared default"
                else:
                    scope = None
                tag = (f' <span class="chip amber" title="{why}">override · {scope}</span>'
                       if scope else ' <span class="dim">default</span>')
                managed_cells.append(html.escape(k) + _fp(slug, k) + tag)
            managed = ", ".join(managed_cells) or '<span class="muted">none declared</span>'
            if unmanaged:
                shown = ", ".join(html.escape(k) + _fp(slug, k) for k in unmanaged[:6])
                more = f' <span class="dim">+{len(unmanaged) - 6} more</span>' if len(unmanaged) > 6 else ''
                um = f'<span class="chip red">{len(unmanaged)} unmanaged</span> {shown}{more}'
            else:
                um = '<span class="na">none</span>'
            # Files column: an edit button per live .env that actually exists on this host. No dead
            # buttons -- absent worktree / no .env is stated instead. Editing + auto-refresh
            # recomputes fingerprints below.
            files = secrets.env_files(wt) if wt else []
            if files:
                files_cell = " ".join(_env_edit_form(f) for f in files)
            elif wt and Path(os.path.expanduser(wt)).is_dir():
                files_cell = '<span class="na">no .env on this host</span>'
            else:
                files_cell = '<span class="na">no local checkout on this machine</span>'
            active = bool(unmanaged)
            dot = f'<span class="dot {"red" if unmanaged else "green"}"></span>'
            count = f'{len(unmanaged)} unmanaged' if unmanaged else f'{len(declared)} declared'
            out.append(_repo_acc("secrets", slug, active, dot, count))
            out.append('<div style="padding:12px 14px;display:grid;grid-template-columns:auto 1fr;gap:7px 14px">'
                       '<span class="dim" title="Foreman tracks these and can rotate them">managed</span>'
                       f'<span>{managed}</span>'
                       '<span class="dim" title="present in the project\'s .env but not declared to Foreman">unmanaged</span>'
                       f'<span>{um}</span>'
                       f'<span class="dim">files</span><span>{files_cell}</span>'
                       '</div></details>')
        if not any_row:
            out.append('<div class="na" style="padding:6px 2px">no env keys declared or found</div>')
        out.append('</div>')
        out.append(
            '<div class="card na" style="margin-top:8px">'
            '<b>Default vs override.</b> One <b>default</b> value is shared by every project that '
            'declares a key. Add an <b>override</b> only when a project (or host) needs a different '
            'value — e.g. a different org\'s API key. Resolution is most-specific-first: '
            'project &rsaquo; host &rsaquo; default.<br>'
            'Values never pass through this dashboard (they\'d hit server logs). Use <b>✎ .env</b> '
            'to open a file in your editor on this host (the dashboard is localhost-only, so values '
            'stay off the wire; set <code>FOREMAN_EDITOR</code> to override the default editor). '
            'After you save, the next auto-refresh recomputes the fingerprints and shared-key '
            'highlighting above. For managed keys, use the CLI, which reads the value from stdin:'
            '<pre style="white-space:pre-wrap;margin:6px 0 0">'
            'secrets set KEY                    # the shared default\n'
            'secrets set KEY --project P        # override for project P\n'
            'secrets set KEY --scope-host H     # override for host H\n'
            'secrets rotate KEY --project P     # replace P\'s value everywhere it\'s used</pre>'
            'To stop overriding and fall back to the default, remove that scoped key from the store.'
            ' See <a href="/help">help</a>.</div></section>')

    # Remember each repo accordion's open/closed across sessions (per browser). The server sets a
    # smart default (open on an active signal); a saved choice overrides it.
    out.append(
        '<script>(function(){var K="f-rd:";'
        'document.querySelectorAll("details.repo-acc").forEach(function(d){'
        'var k=K+d.getAttribute("data-acc")+":"+d.getAttribute("data-repo"),v=localStorage.getItem(k);'
        'if(v==="open")d.open=true;else if(v==="closed")d.open=false;'
        'd.addEventListener("toggle",function(){localStorage.setItem(k,d.open?"open":"closed");});'
        '});'
        # legend: open by default for a first-timer, but remember once collapsed
        'var lg=document.getElementById("legend");'
        'if(lg){if(localStorage.getItem("f-legend")==="closed")lg.open=false;'
        'lg.addEventListener("toggle",function(){localStorage.setItem("f-legend",lg.open?"open":"closed");});}'
        # foldable top sections: open by default, remember each one collapsed per browser
        'document.querySelectorAll("details.sec").forEach(function(s){'
        'var sk="f-sec:"+s.id,sv=localStorage.getItem(sk);'
        'if(sv==="closed")s.open=false;else if(sv==="open")s.open=true;'
        's.addEventListener("toggle",function(){localStorage.setItem(sk,s.open?"open":"closed");});'
        '});'
        '})();</script>')

    # "Since you were away": mark events newer than the last visit, then record this visit.
    # Read-only note: this only personalises highlighting client-side; no server state.
    if not read_only:
        # Mark events newer than the last visit; record "seen" only when the tab is actually
        # HIDDEN (you left), NOT on load -- otherwise the 30s auto-refresh would reset it every
        # cycle and "new" would never show. So new = since you last looked away.
        out.append(
            '<script>(function(){var seen=localStorage.getItem("f-seen")||"",n=0;'
            'document.querySelectorAll(".activity[data-ts]").forEach(function(r){'
            'if(r.getAttribute("data-ts")>seen){r.classList.add("isnew");n++;}});'
            'var c=document.getElementById("newcount");if(c)c.textContent=n?n+" new":"";'
            'document.addEventListener("visibilitychange",function(){if(document.hidden){'
            'localStorage.setItem("f-seen",new Date().toISOString().slice(0,19)+"Z");}});})();'
            '</script>')

    out.append('</div></body></html>')
    return _foldablize("".join(out), secsum, open_ids)


def help_page() -> str:
    """A self-contained user guide, served at /help."""
    body = """
<h1 style="font-size:18px">Foreman — user guide</h1>
<p class="dim">Foreman supervises multiple Claude Code projects: it schedules recurring
review <b>loops</b>, collects a <b>verdict</b> from every run, and shows one board. It
schedules and reads — the loop prompts do the work.</p>

<h2>Getting started</h2>
<ol class="dim">
<li><b>Register a project.</b> Foreman only supervises projects listed in
<code>$FOREMAN_DIR/registry.yaml</code> under <code>projects:</code>. Fastest: point it at the
folder your repos live in — <code>python -m collectors.discover scan ~ --apply</code> registers
every git repo there, matched to its GitHub project by its <code>origin</code> remote (add
<code>--owner NAME</code> to filter). Or register one:
<code>discover register &lt;path&gt;</code>. The <b>Discovered</b> section below has buttons for
both. Then <code>python -m collectors.validate</code> and commit.</li>
<li><b>Enable a loop</b> for it: <code>python -m collectors.loops enable &lt;project&gt; &lt;loop&gt;</code>.</li>
<li><b>Decide auto-run vs queue</b> (each project's row in the <b>Loops</b> panel): auto-run fires
loops unattended within the time-of-day window; queue holds them for the next Claude Code session.</li>
<li><b>Optionally set a frequency and window</b> (in the same Loops panel, or
<code>loops schedule &lt;project&gt; &lt;loop&gt; daily|weekly|…</code>; needs auto-run).</li>
<li><b>Install the scheduler</b>: <code>python -m collectors.scheduler install --mode auto</code>.
The first run writes a receipt; the board fills in.</li>
</ol>

<h2>The board</h2>
<div class="card"><b>Needs you</b> — the escalation queue: red verdicts, ambers aged to red,
and cadences gone stale (no receipt within 2× their schedule). Each carries the run's next
action.</div>
<div class="card"><b>Loops</b> — one panel for everything per-project, as a collapsible accordion
per project (collapsed by default; auto-opens on a red/amber/overdue loop or a queued decision,
choices remembered per browser). A summary line answers the cross-project questions — how many
loops are <b>scheduled</b>, which fires <b>next</b>, how many need a <b>manual</b> launch. Each
project's panel carries its <span class="pill green">auto-run</span>/<span class="pill amber">queue</span>
toggle and time-of-day <b>window</b>, then a row per loop: last run, verdict, <b>schedule</b>
(frequency preset + next fire, or <i>manual</i>), <b>where it runs</b> (this host / cloud /
next session — <i>default</i> follows the loop's own setting), and <b>▶ run</b> / <b>✕ remove</b>.
A <b>+ Add loop</b> control wires a new loop to that project.</div>
<div class="card"><b>Loop library</b> — every loop (built-in / library / custom), who has it
enabled, its safety envelope (<i>writes</i>: PR/files/branch, <i>caps</i>: timeout+budget,
<i>escalates on</i>), and controls to <b>enable</b> it for a project, <b>✎ edit</b> its file, or
<b>Add loop</b> to scaffold a new one. Opt-in per project — each is a recurring run with real
cost — so loops aren't all on by default.</div>
<div class="card"><b>Repo status</b> — live git/GitHub health per project (branch, dirty files,
stale/agent branches, open PRs, failing checks, issues), rolled into a clean / attention / issue
summary; per-project accordions carry the detail.</div>
<div class="card"><b>Drift</b> — config resolver output: pin drift, permission shadowing,
budget breach. <b>Queued</b> — operator decisions waiting. <b>Discovered</b> — projects seen
in session history that aren't registered yet.</div>
<div class="card"><b>API keys &amp; secrets</b> — declared env keys per project, which values are
shared across projects, and whether each uses a per-scope override. Values live in the sops store
and never traverse the dashboard — edit <b>▸ .env</b> opens the file on this host.</div>

<h2>Verdicts</h2>
<p><span class="pill green">green</span> all good ·
<span class="pill amber">amber</span> accumulating, ages to red ·
<span class="pill red">red</span> needs a decision (opens a GitHub issue) ·
<span class="pill stale">stale</span> the scheduler missed it.</p>

<h2>Standard loops</h2>
<p class="dim">docs-sync, quality-review, beta-readiness, harness-refresh (built in) plus
arch-review, security-review, perf-review, prod-readiness, test-review, and
<b>pen-test</b> (authorized, own-project). Enable per project, one click.</p>

<h2>Common commands</h2>
<pre class="card">
python -m collectors.loops list                     # the catalog
python -m collectors.loops enable &lt;project&gt; &lt;loop&gt;    # one-click enable
python -m collectors.loops schedule &lt;project&gt; &lt;loop&gt; daily   # frequency preset (needs auto-run)
python -m collectors.loops new &lt;name&gt; --metrics a,b # scaffold a reusable library loop
python -m collectors.loops status &lt;project&gt;          # last-run per loop
python -m collectors.dispatch &lt;project&gt; &lt;loop&gt;        # fire off-cycle
python -m collectors.discover                        # unregistered projects
python -m collectors.collect                         # run all collectors -> index
python -m collectors.scheduler install --mode auto   # launchd agent honouring autorun
python -m collectors.scheduler install --mode dispatch  # queue everything (no spend)
python -m collectors.secrets set KEY --project P     # per-project key override
</pre>

<h2>Secrets: default vs override</h2>
<p class="dim">A key has one <b>default</b> value shared by every project that declares it in
<code>env_keys</code>. Add an <b>override</b> only when a project or host needs a different
value (e.g. a different org's API key). Resolution is most-specific-first:
<b>project &rsaquo; host &rsaquo; default</b>. The Secrets table tags each key
<span class="dim">default</span> or <span class="pill amber">override · project/host</span>.
Values live encrypted in the sops store and never pass through this dashboard — set them from
the CLI (value read from stdin):</p>
<pre class="card">
python -m collectors.secrets set KEY                 # shared default
python -m collectors.secrets set KEY --project P     # override for one project
python -m collectors.secrets set KEY --scope-host H  # override for one host
python -m collectors.secrets push P                  # write P's resolved keys to its .env
</pre>
<p class="dim">The Secrets table also lists keys found in a project's <code>.env</code> files
that aren't declared (<span class="chip red">N unmanaged</span>) — add them to the project's
<code>env_keys</code> to bring them under management.</p>

<h2>How a run works</h2>
<p class="dim">The scheduler fires a due loop (auto-run) or queues it (queue). A Claude Code
session runs the loop prompt, which measures metrics and writes a partial; the SessionEnd
hook turns that into a receipt on the <code>state</code> branch. Collectors ingest receipts +
git/GitHub/config/telemetry into the index; the board reads it. A red opens a GitHub issue; a
green run closes it with a link to the clearing receipt.</p>
<p><a href="/">← back to the board</a></p>
"""
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Foreman — help</title>'
            f'<style>{CSS} pre{{white-space:pre-wrap;overflow-x:auto}} code{{color:var(--blue)}}'
            f'</style></head><body><div class="wrap">{body}</div></body></html>')


def make_handler(foreman_dir, state_dir, index_path, refresh=30, gh_login=None, fingerprints=True):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - match base signature
            pass

        def _html(self, body, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path.startswith("/health"):
                self._html("ok")
                return
            if path.rstrip("/") == "/help":
                self._html(help_page())
                return
            qs = urllib.parse.parse_qs(parsed.query)
            error = qs.get("err", [None])[0]
            search = qs.get("pq", [None])[0]
            edit = qs.get("edit", [None])[0]
            data = _gather(foreman_dir, state_dir, index_path)
            try:
                self._html(render(data, refresh=refresh, gh_login=gh_login,
                                  fingerprints=fingerprints, error=error, search=search,
                                  edit=edit))
            finally:
                if data["conn"]:
                    data["conn"].close()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            action = self.path.rstrip("/").lstrip("/")
            # A mutating action that raises must be legible, not silently repainted as a normal
            # board: capture the failure, log it, and redirect with ?err=<action> so render()
            # can show a red banner. Especially matters for outward/spend actions (dispatch,
            # close, decision-apply).
            err = None
            try:
                self._do_action(action, form)
            except Exception:
                import traceback
                traceback.print_exc()
                err = action or "unknown"
            loc = "/" if err is None else f"/?err={urllib.parse.quote(err)}"
            self.send_response(303)
            self.send_header("Location", loc)
            self.end_headers()

        def _do_action(self, action, form):
            apply_action(action, form, foreman_dir=foreman_dir, state_dir=state_dir,
                         index_path=index_path)

    return Handler


def apply_action(action, form, *, foreman_dir, state_dir, index_path) -> None:
    """Apply one dashboard mutation. Extracted from the request handler so it is unit-testable
    without spinning up an HTTP server: every POST route lands here. ``form`` is the parsed
    ``parse_qs`` mapping (name -> list of values). Raises on any failure; the caller turns that
    into the ?err= banner."""
    if action == "dispatch":
        registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
        conn = db.open_index(index_path or str(db.default_path()))
        try:
            dispatch.dispatch(conn, registry, state_dir,
                              form["project"][0], form["cadence"][0])
        finally:
            conn.close()
    elif action == "autorun":
        scheduler.set_project_autorun(Path(foreman_dir), form["project"][0],
                                      form["value"][0] == "true")
    elif action == "close":
        conn = db.open_index(index_path or str(db.default_path()))
        try:
            escalations.close(conn, int(form["id"][0]),
                              form.get("reason", ["resolved from dashboard"])[0],
                              foreman_dir=Path(foreman_dir))
        finally:
            conn.close()
    elif action == "loop-schedule":
        # two entry points into the same setter: the "every N days" number input (days=N) and the
        # named-preset dropdown (preset=weekdays|weekly|monthly|default).
        if form.get("days"):
            days = max(1, min(365, int(form["days"][0])))
            preset = f"every-{days}d"
        else:
            preset = form["preset"][0]
        scheduler.set_loop_schedule(Path(foreman_dir), form["project"][0],
                                    form["loop"][0], preset)
    elif action == "loop-tier":
        scheduler.set_loop_tier(Path(foreman_dir), form["project"][0],
                                form["loop"][0], form["tier"][0])
    elif action == "loop-disable":
        loops.disable(Path(foreman_dir), form["project"][0], form["loop"][0])
    elif action == "autorun-window":
        # project="" (or "*") targets the fleet defaults; else that one project.
        proj = (form.get("project", [""])[0] or "").strip()
        minute = (form.get("start_minute", [""])[0] or "").strip()
        scheduler.set_autorun_window(
            Path(foreman_dir), proj or None,
            int(form["start_hour"][0]), int(form["span_hours"][0]),
            int(minute) if minute else None)
    elif action == "env-edit":
        # Whitelist the path against every project's worktrees so this can't be coerced
        # into opening an arbitrary file (traversal), then launch detached on this host.
        registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
        worktrees = [w for p in registry.get("projects", [])
                     for w in (p.get("worktree") or {}).values()]
        target = form.get("path", [""])[0]
        if not secrets.is_live_env_file(target, worktrees):
            raise ValueError(f"{target!r} is not an editable .env on this host")
        _open_env_in_editor(target)
    elif action == "loop-enable":
        project, loop = form["project"][0], form["loop"][0]
        loops.enable(Path(foreman_dir), project, loop)
        # optional settings from the rich add-loop form: apply after the loop is wired.
        tier = (form.get("tier", [""])[0] or "").strip()
        if tier:
            scheduler.set_loop_tier(Path(foreman_dir), project, loop, tier)
        preset = (form.get("preset", [""])[0] or "").strip()
        if preset and preset != "default":
            # a frequency preset needs auto-run; ignore it quietly if the project is in queue mode
            reg = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
            if scheduler.autorun_for(reg, project):
                scheduler.set_loop_schedule(Path(foreman_dir), project, loop, preset)
    elif action == "loop-author":
        # In-dashboard create/edit: writes loops/<name>.yaml (metrics + verdict) and
        # prompts/<name>.md (the instructions) — no external editor.
        loops.author(
            Path(foreman_dir), form.get("name", [""])[0].strip(),
            title=form.get("title", [""])[0],
            metrics=[m.strip() for m in form.get("metrics", [""])[0].split(",") if m.strip()],
            green=form.get("green", [""])[0], amber=form.get("amber", [""])[0],
            prompt=form.get("prompt", [""])[0])
    elif action == "loop-new":
        name = form.get("name", [""])[0].strip()
        metrics = [m.strip() for m in form.get("metrics", [""])[0].split(",")
                   if m.strip()] or None
        title = (form.get("title", [""])[0] or "").strip() or None
        loops.new(Path(foreman_dir), name, metrics=metrics, title=title)
    elif action == "loop-customize":
        # Copy a built-in's spec into the library so it can be edited, then open it.
        res = loops.customize(Path(foreman_dir), form["loop"][0])
        _open_env_in_editor(res["template"])
    elif action == "loop-edit":
        # Whitelist to this instance's cadences/ and loops/ dirs (no traversal), then
        # open detached on this host.
        target = Path(os.path.expanduser(form["path"][0])).resolve()
        allowed = [(Path(foreman_dir) / "cadences").resolve(),
                   (Path(foreman_dir) / "loops").resolve()]
        if not (target.suffix == ".yaml" and target.is_file()
                and any(target.is_relative_to(d) for d in allowed)):
            raise ValueError(f"{target} is not an editable loop file")
        _open_env_in_editor(str(target))
    elif action == "register":
        res = discover.register_project(Path(foreman_dir), form["path"][0],
                                        host=os.environ.get("FOREMAN_HOST", "mbp"))
        if res.get("skipped"):
            raise ValueError(res["skipped"])
    elif action == "register-scan":
        owner = (form.get("owner", [""])[0] or "").strip() or None
        results = discover.register_from_dir(
            Path(foreman_dir), form.get("base", ["~"])[0],
            host=os.environ.get("FOREMAN_HOST", "mbp"), owner=owner, apply=True)
        if not any(r.get("registered") for r in results):
            raise ValueError("no new repos to register under that folder")
    elif action == "decision-apply":
        D.apply_decision(state_dir, Path(foreman_dir), form["id"][0],
                         host=os.environ.get("FOREMAN_HOST", "mbp"))
    elif action == "decision-dismiss":
        D.dismiss(state_dir, form["id"][0])
    elif action == "dispatch-run":
        # Launch a queued dispatch_cadence headlessly on this host now (reuses the scheduler's
        # launch path — the same one the launchd agent uses), then clear it from the queue so it
        # isn't also delivered to a session (double run).
        _, dec = D._find_pending(Path(state_dir), form["id"][0])
        if dec is None or dec["kind"] != "dispatch_cadence":
            raise ValueError("not a pending dispatch_cadence decision")
        project, cadence = dec["project"], dec["payload"]["cadence"]
        host = os.environ.get("FOREMAN_HOST", "mbp")
        registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
        cad = yaml.safe_load((Path(foreman_dir) / "cadences" / f"{cadence}.yaml").read_text())
        tier = scheduler.effective_tier(registry, project, cadence, cad.get("tier")) or "local"
        spool = Path(os.path.expanduser(
            (registry.get("defaults") or {}).get("spool_dir") or "~/foreman/spool"))
        conn = db.open_index(index_path or str(db.default_path()))
        try:
            res = scheduler._launch_run(Path(foreman_dir), Path(state_dir), spool, conn,
                                        project=project, cadence=cadence, tier=tier, host=host,
                                        now=_now())
        finally:
            conn.close()
        if res.get("skipped"):
            raise ValueError(f"could not launch {cadence} on {host}: {res['skipped']}")
        D.dismiss(Path(state_dir), form["id"][0])   # launched -> remove from the queue
    else:
        raise ValueError(f"unknown action {action!r}")


def main(argv: list[str] | None = None) -> int:
    from http.server import ThreadingHTTPServer
    ap = argparse.ArgumentParser(prog="web")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--refresh", type=int, default=int(os.environ.get("FOREMAN_WEB_REFRESH", "30")),
                    help="page auto-refresh seconds; 0 to disable")
    ap.add_argument("--secret-fingerprints", action=argparse.BooleanOptionalAction, default=True,
                    help="show the last-5 chars of each key's .env value (a fingerprint)")
    args = ap.parse_args(argv)
    login = gh_account()
    handler = make_handler(Path(args.foreman_dir), Path(args.state_dir), args.index,
                           args.refresh, login, args.secret_fingerprints)
    print(f"Foreman dashboard on http://localhost:{args.port}  (gh: {login or 'not authed'})")
    ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
