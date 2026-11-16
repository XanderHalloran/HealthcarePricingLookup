"""Consumer price guide: chrome, trust pages, hubs, cash directory, review queue.
pytest OR `python tests/test_guide.py`. Uses whatever local data exists."""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("COMMUNITY_DB", os.path.join(tempfile.mkdtemp(), "community.sqlite"))

from fastapi.testclient import TestClient  # noqa: E402
import app as webapp  # noqa: E402

client = TestClient(webapp.app)


def test_trust_pages_exist_and_share_chrome():
    for path in ("/privacy", "/trust", "/faq", "/about", "/corrections", "/data-coverage", "/procedures"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'class="gnav"' in r.text and 'class="tfoot"' in r.text, path   # header + trust footer
        assert "No paid placement" in r.text, path


def test_old_404s_now_resolve():
    assert client.get("/phoenix", follow_redirects=False).headers["location"] == "/arizona/phoenix"
    assert client.get("/hospitals", follow_redirects=False).headers["location"] == "/arizona/hospitals"
    for path in ("/arizona", "/arizona/phoenix", "/arizona/tucson", "/arizona/hospitals"):
        assert client.get(path).status_code == 200, path
    assert client.get("/not-a-place").status_code == 404
    assert client.get("/nevada").status_code == 404                    # configured, no data: honest 404


def test_search_by_code_and_word():
    r = client.get("/search", params={"q": "73721"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/procedure/73721"
    assert "/procedure/73721" in client.get("/search", params={"q": "knee mri"}).text


def test_procedure_page_counts_agree_and_crumbs():
    t = client.get("/phoenix/procedure/73721").text
    counts = set(re.findall(r"(\d+) facilities \(", t))
    assert len(counts) == 1, counts                                    # one facility count everywhere
    assert t.split("<title>")[1].split("</title>")[0].endswith(webapp.BRAND["short"].replace("&", "&amp;"))
    crumb = t.split('class="crumb"')[1].split("</nav>")[0]
    assert "Arizona" in crumb and "Phoenix Metro" in crumb and "CPT 73721" in crumb


def test_methodology_title_and_vintage():
    t = client.get("/methodology").text
    title = t.split("<title>")[1].split("</title>")[0]
    assert title.endswith("| " + webapp.BRAND["short"].replace("&", "&amp;"))
    assert "September 2026" not in t and webapp._vintage() in t


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
