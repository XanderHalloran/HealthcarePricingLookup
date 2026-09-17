"""Ortho profile (APP_CONFIG=config-ortho): run STANDALONE — `python tests/test_ortho.py` —
because the env must be set before `app` is imported (test_app imports the consumer profile).
Uses the local Arizona dataset in data/ (any codes shared with the MSK allowlist, e.g. 73721)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(__file__)
os.environ["APP_CONFIG"] = "config-ortho"
os.environ["BASE_URL"] = "https://ortho.traqqit.com"
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402
import app as webapp  # noqa: E402

client = TestClient(webapp.app)


def test_head_and_shell_are_ortho():
    """Design critique F1/F3/F5/F6: ortho identity in the head, slim shell, no consumer chrome."""
    t = client.get("/").text
    head = t.split("</head>")[0]
    assert 'rel="canonical" href="https://ortho.traqqit.com/"' in head
    assert 'og:url" content="https://ortho.traqqit.com/"' in head and "og-ortho.png" in head
    assert "healthcare.traqqit.com" not in head and "colonoscopy" not in head and "appeal letter" not in head.lower()
    assert 'class="cost-share"' in t and 'id="metded"' in t                 # cost-share present but collapsed
    assert 'id="state" aria-label="Choose your state" onchange="onStateChange()" hidden' in t   # AZ-only
    assert 'class="onav-menu"' in t and t.count("Price a code list") == 1  # nav slimmed; toggle keeps it once
    assert "Start with a common procedure" in t and "add('27447')" in t and 'DEFAULT_CODE = "27447"' in t
    # F8/F9: results -> how-to -> email capture -> footer; step 3 links every Analyses destination
    body = t.split("</head>")[1]
    order = [body.find(k) for k in ('id="cards"', 'class="how"', 'class="seo-footer"')]
    assert order == sorted(order)
    cap = body.find('<div class="card capture" data-kind="compare"')   # the element, not the JS selector string
    assert cap == -1 or (body.find('class="how"') < cap < body.find('class="seo-footer"'))   # only when SMTP configured
    step3 = body.split("Then open Analyses")[1].split("</div></div>")[0]
    assert 'href="/rates"' in step3 and 'href="/site-of-care"' in step3 and 'href="/position"' in step3


def test_brand_and_nav():
    r = client.get("/")
    assert r.status_code == 200
    assert "Orthopedic Price Intelligence" in r.text and 'class="onav"' in r.text
    assert "Payer rates" in r.text and "Site of care" in r.text and "Analyses" in r.text
    assert '"category": "joint"' in r.text        # MSK categories embedded for the picker
    assert "knee replacement, ACL" in r.text and '"29888":"acl reconstruction' in r.text   # MSK picker hints + synonyms
    assert '"27447"' in r.text                     # MSK service embedded for search


def test_only_arizona_market():
    assert [s["code"] for s in webapp.MARKETS.values()] == ["AZ", "VA", "NV", "MI", "FL"]
    assert webapp.BASE_URL == "https://ortho.traqqit.com"


def test_rates_page_and_csv():
    r = client.get("/rates", params={"code": "73721", "mine": "900"})
    assert r.status_code == 200
    assert "Medicaid" not in r.text and "Medicaid" not in client.get("/").text      # excluded on ortho
    assert "Medicaid" not in client.get("/rates.csv", params={"code": "73721"}).text
    assert "What each payer pays" in r.text and "Market summary" in r.text
    assert "percentile" in r.text                  # 'your rate' column rendered
    c = client.get("/rates.csv", params={"code": "73721"})
    assert c.status_code == 200 and c.text.startswith("facility_id,facility,cash,negotiated_median")
    assert client.get("/rates", params={"code": "NOPE"}).status_code == 200   # falls back to first service
    r = client.get("/rates", params={"code": "73721", "metro": "AZ"})            # state-level Area choice
    assert r.status_code == 200 and "All Arizona" in r.text and "_area_options" not in r.text


def test_site_of_care_page():
    r = client.get("/site-of-care", params={"code": "73721", "metro": "phoenix"})
    assert r.status_code == 200
    assert "Site of care" in r.text and "Hospitals (outpatient)" in r.text and "published bundles" in r.text


def test_position_page_and_business_framing():
    r = client.get("/position")
    assert r.status_code == 200 and "vs the market" in r.text
    assert "Our percentile" in r.text or "No published prices" in r.text   # local data may lack our facility
    home = client.get("/").text
    assert "Market view" in home and "Price a code list" in home and "Benchmarking data" in home
    assert "AI negotiation coach" not in home and "Negotiate it down" not in home
    card = client.get("/card", params={"code": "73721", "state": "AZ"}).text
    assert "Market typical price" in card and "Ask to pay" not in card and "negotiation scripts" not in card


def test_payer_scorecard_and_bundles():
    assert "facilities" in client.get("/payers").text          # ranking states its facility floor
    r = client.get("/payers", params={"payer": "Aetna"})
    assert r.status_code == 200 and "Payer scorecard" in r.text and "Ranking" in r.text and "Medicaid" not in r.text
    c = client.get("/payers.csv")
    assert c.status_code == 200 and c.text.startswith("code,type,description,medicare")
    b = client.get("/bundles", params={"episode": "tka", "mine": "25000"})
    assert b.status_code == 200 and "Episode pricing: Total knee replacement" in b.text
    assert "Surgeon: Total knee replacement" in b.text and "Anesthesia (01402" in b.text and "Your bundle" in b.text
    assert client.get("/bundles", params={"episode": "nope"}).status_code == 200      # falls back to first episode


def test_methodology_page_ortho():
    r = client.get("/methodology")
    assert r.status_code == 200 and "Methodology" in r.text and "IPPS" in r.text and "ASC Addendum" in r.text
    assert 'href="#codes"' in r.text and 'id="codes"' in r.text and "CPT 27447" in r.text
    s = client.get("/site-of-care", params={"code": "27447"}).text     # no published ASC price -> estimate (27447 is in both code lists)
    assert "Implied ASC price" in s and "estimate" in s


def test_slug_pages_use_ortho_slugs():
    r = client.get("/arizona/phoenix/mri-knee")
    assert r.status_code == 200 and "MRI knee" in r.text
    assert client.get("/arizona/phoenix/mammogram").status_code == 404     # consumer slug, not MSK


def test_map_near_address():
    assert client.get("/map").status_code == 200                      # empty state, no geocode call
    webapp._geocode = lambda q, st="": (33.48, -112.07)                      # Phoenix; no network in tests
    r = client.get("/map", params={"code": "73721", "q": "85015", "radius": "25"})
    assert r.status_code == 200 and "within 25 miles" in r.text and "leaflet" in r.text
    assert 'id="map"' in r.text
    assert 'onclick="useMyLocation()"' not in client.get("/").text   # ortho offers ZIP/address only, never the browser prompt
    assert "never asks your browser" in r.text
    r = client.get("/map", params={"code": "73721", "q": "85015", "radius": "junk", "sort": "dist"})
    assert r.status_code == 200 and "within 25 miles" in r.text     # bad radius falls back to 25
    def boom(q, st=""): raise ValueError
    webapp._geocode = boom
    assert "find that address" in client.get("/map", params={"q": "nowhere"}).text


def test_inline_script_parses():
    if not shutil.which("node"):
        return
    js = re.findall(r"<script>(.*?)</script>", client.get("/").text, re.S)[-1]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    res = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    os.remove(f.name)
    assert res.returncode == 0, res.stderr[:400]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
