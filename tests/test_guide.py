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


def test_cash_directory_filters_and_labels():
    r = client.get("/cash")
    assert r.status_code == 200 and "Prepaid bundle (MDsave)" in r.text
    assert "source document dated Feb 2025" in r.text                 # AAI PDF vintage shown
    t = client.get("/cash", params={"procedure": "mri", "city": "phoenix"}).text
    prices = [float(x.replace(",", "")) for x in re.findall(r'class="r money">(?:from )?\$([\d,.]+)', t)]
    assert prices and prices == sorted(prices)                         # price order, nothing else


def test_cash_rank_is_price_then_quality_only():
    import cashpay
    rows = cashpay.for_code(webapp.CASH, "73721", "AZ", "phoenix")
    key = [(r["price"] is None, r["price"] or 0) for r in rows]
    assert key == sorted(key)


def test_shared_address_providers_stay_separate():
    names = [p["name"] for p in webapp.CASH["providers"]]
    assert any(n.startswith("RadiologyAssist") for n in names) and "SimonMed Imaging - 19th Avenue" in names


def test_procedure_page_shows_cash_beside_hospitals_and_links_profiles():
    t = client.get("/phoenix/procedure/73721").text
    assert "Independent cash-pay prices for CPT 73721" in t
    links = set(re.findall(r'class="fac-n"><a href="([^"]+)"', t))
    assert links and all(client.get(u).status_code == 200 for u in links)


def test_every_cash_provider_has_a_profile():
    for p in webapp.CASH["providers"]:
        assert client.get(webapp._cash_url(p)).status_code == 200, p["slug"]


def test_correction_goes_to_review_queue_not_the_site():
    import community
    r = client.post("/corrections", data={"kind": "wrong_price", "facility": "Test Imaging", "procedure": "73721",
                                          "price": "$199", "source_url": "example.com/prices", "role": "patient"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/corrections?sent=" in r.headers["location"]
    q = community.pending()
    assert q and q[-1]["facility"] == "Test Imaging" and q[-1]["status"] == "pending"
    assert q[-1]["source_url"] == "https://example.com/prices"
    assert "Test Imaging" not in client.get("/cash").text              # nothing auto-publishes
    assert client.post("/corrections", data={"kind": "nope", "facility": "x"}).status_code == 400
    bot = client.post("/corrections", data={"kind": "other", "facility": "Spam", "details": "x",
                                            "website": "http://spam"}, follow_redirects=False)
    assert bot.status_code == 303 and all(s["facility"] != "Spam" for s in community.pending())


def test_newsletter_signup_is_stored_locally():
    import community, sqlite3
    r = client.post("/newsletter", data={"email": "Someone@Example.com", "page": "/cash"})
    assert r.status_code == 200 and "on the list" in r.text
    with sqlite3.connect(community.db_path()) as con:
        assert con.execute("select count(*) from signups where email='someone@example.com'").fetchone()[0] == 1
    assert client.post("/newsletter", data={"email": "bad"}).status_code == 400


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
