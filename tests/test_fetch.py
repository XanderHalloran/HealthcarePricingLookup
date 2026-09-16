"""fetch_mrf cache revalidation. pytest OR `python tests/test_fetch.py`.

The bug this guards: a plain "file exists -> skip" makes the monthly refresh a
no-op, silently re-ingesting stale bytes forever. So the check that matters is
that a CHANGED file is actually re-downloaded, and an unchanged one is not.
Uses a stdlib http.server so nothing hits the network.
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from pipeline import fetch_mrf  # noqa: E402

STATE = {"body": b"v1", "etag": '"aaa"', "hits": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        STATE["hits"] += 1
        if self.headers.get("If-None-Match") == STATE["etag"]:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", STATE["etag"])
        self.send_header("Content-Length", str(len(STATE["body"])))
        self.end_headers()
        self.wfile.write(STATE["body"])

    def log_message(self, *a):
        pass


def _serve(body=b"v1", etag='"aaa"'):
    STATE.update(body=body, etag=etag, hits=0)   # tests share STATE; reset per server
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/mrf.csv"


def test_unchanged_reuses_cache_and_changed_refetches():
    srv, url = _serve()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "raw", "H.csv")

            fetch_mrf(url, dest)
            assert open(dest, "rb").read() == b"v1"
            assert os.path.exists(dest + ".meta")            # validators recorded
            assert STATE["hits"] == 1

            # Same ETag -> server 304s -> cached bytes kept, but we DID revalidate.
            fetch_mrf(url, dest)
            assert open(dest, "rb").read() == b"v1"
            assert STATE["hits"] == 2                        # not skipped blindly

            # Hospital republishes: new ETag must win over the cache.
            STATE.update(body=b"v2-republished", etag='"bbb"')
            fetch_mrf(url, dest)
            assert open(dest, "rb").read() == b"v2-republished"
    finally:
        srv.shutdown()


def test_failed_fetch_leaves_good_cache_intact():
    srv, url = _serve(body=b"good", etag='"ccc"')
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "raw", "H.csv")
            fetch_mrf(url, dest)
            srv.shutdown()                                   # host goes down
            try:
                fetch_mrf(url + "?x", dest)
            except Exception:
                pass
            assert open(dest, "rb").read() == b"good"        # cache survived
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("\n2 passed")


def test_local_file_entry_can_be_a_zip(tmp_path=None):
    """file: + unzip: true resolves to (zip, largest member) exactly like a downloaded zip."""
    import os, tempfile, zipfile
    import pipeline
    d = tempfile.mkdtemp()
    z = os.path.join(d, "X.zip")
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("small.csv", "a\n")
        zf.writestr("big.csv", "hospital_name\n" * 50)
    old_root, pipeline.ROOT = pipeline.ROOT, d
    try:
        assert pipeline._resolve_source({"id": "X", "file": "X.zip", "unzip": True}, d) == (z, "big.csv")
        assert pipeline._resolve_source({"id": "X", "file": "X.zip"}, d) == z
    finally:
        pipeline.ROOT = old_root
