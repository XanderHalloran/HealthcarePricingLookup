"""Smoke test for the web app. pytest OR `python tests/test_app.py`.
Assumes data/hospital_rates is populated (run src/pipeline.py first)."""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402
import app as webapp  # noqa: E402

client = TestClient(webapp.app)


def test_index_shows_form_and_disclaimer():
    r = client.get("/")
    assert r.status_code == 200
    assert "Negotiation Coach" in r.text
    assert "Estimates only" in r.text
    assert "not a guarantee" in r.text.lower()


def test_analyze_renders_pricing_and_target():
    # Structural assertions only — independent of which Medicare/hospital data is
    # loaded (real reference data vs fixtures). We assert the page renders the code,
    # echoes the charge, and produces coaching, not specific dollar amounts.
    r = client.post("/analyze", data={"codes": "73721 MRI lower extremity 2,400.00", "state": "AZ"})
    assert r.status_code == 200
    assert "73721" in r.text
    assert "2,400.00" in r.text                           # charged amount echoed (in textarea)
    assert "Medicare benchmark" in r.text                 # results rendered
    assert "fair self-pay target" in r.text or "itemized bill" in r.text   # talking points


def test_index_shows_shop_picker():
    r = client.get("/")
    assert r.status_code == 200
    assert "Shop a price" in r.text and "Analyze my bill" in r.text   # both tabs
    assert 'id="svc-search"' in r.text                               # code/name search box
    assert '"73721"' in r.text                                       # service data embedded for search
    assert "MRI lower extremity" in r.text                           # a service rendered


def test_card_endpoint_renders_single_card():
    r = client.get("/card", params={"code": "73721", "state": "AZ"})
    assert r.status_code == 200
    assert "73721" in r.text
    # Assert on the card template's own copy (_card.html), not the analyze page's:
    # the card shows "What Medicare pays" + a copyable ask amount, and a facility table.
    assert "What Medicare pays" in r.text and 'class="ask-amt"' in r.text
    assert "Facility" in r.text                          # per-hospital price table rendered


def test_card_endpoint_unknown_code():
    r = client.get("/card", params={"code": "ZZZZZ", "state": "AZ"})
    assert r.status_code == 200
    assert "No data" in r.text


def test_shop_codes_flow():
    # "Shop a price" tab posts selected service codes (no charges)
    r = client.post("/analyze", data={"shop_codes": ["73721", "99284"], "state": "AZ"})
    assert r.status_code == 200
    assert "73721" in r.text and "99284" in r.text
    assert "Medicare benchmark" in r.text
    assert 'class="ask-amt"' in r.text                 # the "Ask to pay $X" hero number


def test_empty_submission_shows_guidance():
    r = client.post("/analyze", data={"codes": "", "state": "AZ"})
    assert r.status_code == 200
    assert "No billing codes found" in r.text       # friendly empty-state, not a blank page


def test_letter_endpoint_downloads_text():
    r = client.post("/letter", data={"codes": "73721 MRI 3200", "state": "AZ"})
    assert r.status_code == 200
    assert "Billing Department" in r.text and "73721" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")


def test_analyze_shows_issues_for_duplicates():
    r = client.post("/analyze", data={"codes": "99284 ER 2000\n99284 ER 2000", "state": "AZ"})
    assert r.status_code == 200
    assert "to check on this bill" in r.text          # issues banner rendered
    assert "duplicate" in r.text.lower()


def test_procedure_seo_page():
    r = client.get("/procedure/73721")
    assert r.status_code == 200
    assert "How much does" in r.text and "73721" in r.text and "canonical" in r.text


def test_procedure_unknown_404():
    assert client.get("/procedure/ZZZZZ").status_code == 404   # not a code pattern -> unresolved


def test_sitemap_lists_procedures():
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and "/procedure/73721" in r.text and "urlset" in r.text


def test_payer_control_and_card_data():
    home = client.get("/").text
    assert 'id="payer"' in home and ">Aetna<" in home          # payer dropdown rendered
    assert "onPayerChange" in home and "applyDeductible" in home
    card = client.get("/card", params={"code": "73721", "state": "AZ"}).text
    assert "data-payer=" in card and "payer-line" in card      # card carries payer data
    # passing a payer is accepted (prices would reflect it when data is present)
    assert client.get("/card", params={"code": "73721", "state": "AZ", "payer": "Aetna"}).status_code == 200


def test_eob_endpoint_graceful_without_key():
    old = os.environ.pop("ANTHROPIC_API_KEY", None)   # ensure the paid API isn't called
    try:
        r = client.post("/eob", files={"file": ("bill.png", b"\x89PNG fake", "image/png")})
        assert r.status_code == 200
        assert "isn't enabled" in r.text or "no billing codes" in r.text
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_analyze_flags_unreadable_line():
    r = client.post("/analyze", data={"codes": "this is not a code", "state": "MA"})
    assert r.status_code == 200
    assert "Couldn&#39;t read" in r.text or "Couldn't read" in r.text


def test_home_has_metro_selector_and_seo():
    home = client.get("/").text
    assert 'id="metro"' in home and "onMetroChange" in home           # metro control + handler
    assert ">Tucson Metro<" in home and ">Phoenix Metro<" in home      # metro options rendered
    assert 'property="og:title"' in home and 'rel="canonical"' in home # social + canonical meta
    assert "application/ld+json" in home and "seo-footer" in home      # structured data + link footer
    assert "/tucson/procedure/" in home                               # internal links to metro pages


def test_metro_procedure_page():
    r = client.get("/tucson/procedure/73721")
    assert r.status_code == 200
    assert "Tucson" in r.text and "canonical" in r.text
    assert "/tucson/procedure/73721" in r.text                        # metro-scoped canonical
    assert "application/ld+json" in r.text and "FAQPage" in r.text     # JSON-LD incl. FAQ schema
    assert "Frequently asked questions" in r.text                     # visible FAQ
    assert client.get("/notametro/procedure/73721").status_code == 404


def test_card_accepts_metro_param():
    assert client.get("/card", params={"code": "73721", "state": "AZ", "metro": "tucson"}).status_code == 200


def test_robots_and_sitemap_metro():
    rob = client.get("/robots.txt")
    assert rob.status_code == 200 and "Sitemap:" in rob.text and "sitemap.xml" in rob.text
    sm = client.get("/sitemap.xml").text
    assert "/phoenix/procedure/73721" in sm and "/tucson/procedure/73721" in sm



def test_slug_seo_page_and_canonical():
    r = client.get("/arizona/phoenix/mri-brain")
    assert r.status_code == 200
    assert "MRI brain" in r.text and "Phoenix" in r.text
    assert 'rel="canonical" href="https://healthcare.traqqit.com/arizona/phoenix/mri-brain"' in r.text
    # the code-URL twin canonicalizes to the readable URL (no duplicate content)
    r2 = client.get("/phoenix/procedure/70551")
    assert r2.status_code == 200 and "/arizona/phoenix/mri-brain" in r2.text
    assert client.get("/arizona/phoenix/not-a-procedure").status_code == 404
    assert client.get("/arizona/tucson/mri-brain").status_code == 200


def test_state_switch_data_is_embedded_for_client_side_area_list():
    """The Area list must follow the state without a reload: every live metro is embedded."""
    r = client.get("/")
    assert r.status_code == 200
    assert '"key": "phoenix"' in r.text and '"state": "AZ"' in r.text
    assert "function onStateChange" in r.text and "METROS.filter" in r.text
    r2 = client.get("/?state=CA") if "CA" in r.text else None    # only when CA data exists locally
    if r2 is not None and 'value="CA" selected' in r2.text:
        assert "All California" in r2.text and "Phoenix Metro" not in r2.text.split("<select id=\"metro\"")[1].split("</select>")[0]


def test_inline_script_parses():
    """A single JS syntax error silently kills search, state switch, everything. Guard it."""
    import re, shutil, subprocess, tempfile
    if not shutil.which("node"):
        return
    js = re.findall(r"<script>(.*?)</script>", client.get("/").text, re.S)[-1]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    res = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    os.remove(f.name)
    assert res.returncode == 0, res.stderr[:400]


def test_methodology_page():
    r = client.get("/methodology")
    assert r.status_code == 200
    assert "How the numbers are built" in r.text and "<svg" in r.text and "Data flow" in r.text
    assert "facilities with prices" in r.text and "Tukey" in r.text
    assert "/methodology" in client.get("/sitemap.xml").text


def test_coming_soon_state_is_honest_404():
    r = client.get("/nevada/las-vegas/mri-brain")
    assert r.status_code == 404 and "coming soon" in r.text.lower() and "$" not in r.text
    assert client.get("/nevada/procedure/73721").status_code == 404
    assert 'value="NV"' not in client.get("/").text            # hidden from the selector until data lands


def test_sitemap_lists_slug_pages():
    r = client.get("/sitemap.xml")
    assert "https://healthcare.traqqit.com/arizona/phoenix/mri-brain" in r.text
    assert "/nevada/" not in r.text


def test_email_capture_is_optional_and_validates():
    r = client.post("/email", data={"email": "nope", "kind": "letter", "codes": "73721", "state": "AZ"})
    assert r.status_code == 400
    r = client.post("/email", data={"email": "a@b.co", "kind": "compare", "codes": "73721", "state": "AZ"})
    assert r.status_code == 200 and "isn't enabled" in r.text      # no SMTP in tests -> graceful, never a wall
    assert "Download letter" in client.post("/analyze", data={"codes": "73721 MRI 2400.00", "state": "AZ"}).text


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
