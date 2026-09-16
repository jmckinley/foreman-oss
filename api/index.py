"""Vercel Python entrypoint: a read-only hosted view of the Foreman board.

Foreman's real dashboard is a stateful local server (``python -m collectors.web``) that runs
git/subprocess actions. Vercel is serverless and read-only, so this renders the board *only*
from what is committed to the repo -- ``registry.yaml`` and the ``index.db`` cache -- with
every action control hidden. ``state/`` (receipts, the decision queue) is gitignored and so
absent here; the board tolerates that and shows what the index holds.

The deployment filesystem is read-only except ``/tmp``; ``db.connect`` may ALTER the index to
self-heal columns, so the committed ``index.db`` is copied to ``/tmp`` and opened there.
"""

from __future__ import annotations

import hmac
import http.cookies
import os
import shutil
import urllib.parse
from pathlib import Path

from collectors import web

ROOT = Path(__file__).resolve().parent.parent

# App-level access gate. Vercel Hobby does not reliably protect production behind Vercel
# Authentication, and this board exposes operational data (roster, repos, drift, secret key
# NAMES), so it gates itself on a shared token set in the Vercel project env. Fail closed: if
# no token is configured, nothing is served -- the board is never accidentally public.
_TOKEN_ENV = "FOREMAN_DASH_TOKEN"
_COOKIE = "fdash"


def _configured_token() -> str | None:
    tok = os.environ.get(_TOKEN_ENV)
    return tok if tok else None


def _presented_token(environ) -> str | None:
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookie = http.cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
    if _COOKIE in cookie:
        return cookie[_COOKIE].value
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    if qs.get("token"):
        return qs["token"][0]
    return None


def _authorized(environ) -> bool:
    want = _configured_token()
    if not want:
        return False                       # fail closed: unconfigured => deny everyone
    got = _presented_token(environ)
    return bool(got) and hmac.compare_digest(got, want)


def _index_path() -> str | None:
    src = ROOT / "index.db"
    if not src.is_file():
        return None
    tmp = Path("/tmp/foreman-index.db")
    try:
        if not tmp.exists() or tmp.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, tmp)
        return str(tmp)
    except OSError:
        return None


def _render() -> bytes:
    data = web._gather(ROOT, ROOT / "state", _index_path())
    try:
        html = web.render(data, refresh=0, read_only=True)
    finally:
        if data.get("conn"):
            data["conn"].close()
    return html.encode("utf-8")


def app(environ, start_response):
    if environ.get("REQUEST_METHOD", "GET").upper() != "GET":
        start_response("405 Method Not Allowed", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"read-only hosted view; manage from the local foreman dashboard"]
    if not _authorized(environ):
        hint = ("access token required — append ?token=... (set FOREMAN_DASH_TOKEN in the "
                "Vercel project env)") if _configured_token() else \
               "dashboard is not configured (no FOREMAN_DASH_TOKEN set); access denied"
        start_response("401 Unauthorized", [("Content-Type", "text/plain; charset=utf-8"),
                                            ("WWW-Authenticate", 'Bearer realm="foreman"')])
        return [hint.encode()]
    try:
        body = _render()
    except Exception as exc:  # never 500 with a stack trace to the URL
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [f"render failed: {type(exc).__name__}".encode()]
    headers = [("Content-Type", "text/html; charset=utf-8"),
               ("Cache-Control", "no-store")]
    # Persist the token as a cookie so navigation works after the first ?token= visit.
    tok = _presented_token(environ)
    if tok:
        c = http.cookies.SimpleCookie()
        c[_COOKIE] = tok
        c[_COOKIE]["path"] = "/"
        c[_COOKIE]["max-age"] = 60 * 60 * 24 * 30
        c[_COOKIE]["httponly"] = True
        c[_COOKIE]["secure"] = True
        c[_COOKIE]["samesite"] = "Lax"
        headers.append(("Set-Cookie", c[_COOKIE].OutputString()))
    start_response("200 OK", headers)
    return [body]
