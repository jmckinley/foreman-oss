"""The web request handler end-to-end (make_handler's do_GET/do_POST) over a real ephemeral
server — exercises routing, the 303-redirect-after-action, and the ?err= banner path that the
apply_action unit tests don't reach."""
import contextlib
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import yaml

from collectors import web, db, scheduler


@contextlib.contextmanager
def _serve(foreman_dir, state_dir, index_path):
    handler = web.make_handler(foreman_dir, state_dir, index_path, refresh=0,
                               gh_login=None, fingerprints=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:   # urllib follows the 303 to GET /
        return r.status, r.geturl(), r.read().decode()


def test_get_routes(foreman_dir, state_dir, index_path):
    db.open_index(index_path).close()
    with _serve(foreman_dir, state_dir, index_path) as base:
        code, body = _get(base + "/")
        assert code == 200 and "FOREMAN" in body and "Since you were away" in body
        assert _get(base + "/health")[0] == 200
        code, body = _get(base + "/help")
        assert code == 200 and "user guide" in body.lower()
        _, body = _get(base + "/?err=dispatch")           # error banner path
        assert "Dispatch failed" in body


def test_post_action_redirects_and_applies(foreman_dir, state_dir, index_path):
    db.open_index(index_path).close()
    with _serve(foreman_dir, state_dir, index_path) as base:
        # a real mutating POST: toggle auto-run, follow the 303 back to the board
        code, final_url, _ = _post(base + "/autorun", {"project": "acmeapi", "value": "true"})
        assert code == 200 and final_url.rstrip("/") == base    # 303 -> GET / (the board)
        reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
        assert scheduler.autorun_for(reg, "acmeapi") is True          # the effect landed

        # an unknown/failed action redirects to /?err=<action>, not a 500
        _, final_url, body = _post(base + "/bogus-action", {})
        assert "err=bogus-action" in final_url


def test_post_failed_action_shows_banner(foreman_dir, state_dir, index_path):
    db.open_index(index_path).close()
    with _serve(foreman_dir, state_dir, index_path) as base:
        # loop-tier with a bad tier raises inside the handler -> ?err=loop-tier banner
        _, final_url, body = _post(base + "/loop-tier",
                                   {"project": "acmeapi", "loop": "docs-sync", "tier": "bogus"})
        assert "err=loop-tier" in final_url
        assert "where this loop runs was not updated" in body
