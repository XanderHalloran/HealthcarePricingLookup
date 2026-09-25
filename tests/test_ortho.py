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


def test_referral_finder():
    """Care-management view: payer pricing, contact details, patient cost, hand-off."""
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    r = client.get("/refer")
    assert r.status_code == 200 and "Find a facility for a patient" in r.text
    r = client.get("/refer", params={"code": "73721", "q": "85015", "radius": "25",
                                     "ded": "500", "coins": "20"})
    assert r.status_code == 200
    assert "Patient pays approx" in r.text or "No facility within" in r.text
    assert "copyOptions" in r.text                      # hand-off action present
    # patient-cost math: deductible first, then coinsurance on the rest
    assert webapp._patient_cost(1000, 500, 20) == 600   # 500 + 20% of 500
    assert webapp._patient_cost(300, 500, 20) == 300    # all inside the deductible
    assert webapp._patient_cost(1000, 0, 20) == 200     # deductible met
    assert webapp._patient_cost(1000, None, None) is None
    assert webapp._patient_cost(None, 500, 20) is None
    # every hospital can be handed to a patient
    import yaml
    man = yaml.safe_load(open("config-ortho/hospitals.yaml", encoding="utf-8"))["hospitals"]
    contact = webapp.STATE["contact"]
    missing = [h["id"] for h in man if not (contact.get(h["id"]) or {}).get("phone")]
    assert not missing, f"no phone for {missing[:5]}"
    for k in ("sort=quality", "Best hip/knee"):
        assert k in client.get("/map", params={"code": "73721", "q": "85015"}).text


def test_hopco_theme_is_ortho_only():
    """The recolor is a server-side Jinja gate, not a body class: five ortho templates render
    <body> with no class, so a class scope would leave them green."""
    t = client.get("/").text
    assert "#003A70" in t and "Manrope" in t              # HOPCo navy + typeface
    for path in ("/rates?code=73721", "/site-of-care?code=73721", "/position", "/methodology",
                 "/procedure/73721", "/payers", "/bundles?episode=tka", "/map", "/refer"):
        assert "#003A70" in client.get(path).text, f"{path} did not get the HOPCo theme"
    # the base sheet had no .fac-t rule at all, so seven ortho pages rendered default tables
    assert ".fac-t {" in t and ".mute { color:var(--mute); }" in t


def test_map_actually_initialises():
    """Regression: `L.map(id, {})` with no center/zoom never calls setView, so every addLayer
    defers and `ring.getBounds()` throws -- the map rendered as a blank grey box in production
    with 0 tiles and 0 markers. Nothing in the old suite caught it, because nothing ran the JS."""
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    t = client.get("/map", params={"code": "73721", "q": "85015", "radius": "25"}).text
    js = t.split("L.map('map'")[1].split(")")[0]
    assert "center:" in js and "zoom:" in js, f"L.map must be given a view at init, got: {js}"
    # and the fit must frame the results, not the search ring
    assert "ring.getBounds()" not in t and "fitBounds(L.latLngBounds(pts)" in t


def test_map_never_puts_a_facility_name_into_html():
    """`|tojson` escapes for the JS string context only. Both divIcon({html}) and
    bindPopup(string) are innerHTML sinks, and 23 configured names already contain ' or &."""
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    t = client.get("/map", params={"code": "73721", "q": "85015"}).text
    assert "bindPopup" not in t                                   # the sink is gone entirely
    assert "textContent" in t and "createElement" in t            # markers are built as DOM
    assert "${f.name}" not in t and "${name}" not in t            # never interpolated into markup


def test_zip_picks_the_market_so_four_states_are_not_unreachable():
    """A Richmond ZIP priced against Arizona returned a confident 'no facility within 25 miles'.
    The searched point now selects the market from the 301 coordinates already in memory."""
    assert webapp._market_near((33.48, -112.07)) == "AZ"      # Phoenix
    assert webapp._market_near((37.54, -77.44)) == "VA"       # Richmond
    assert webapp._market_near((36.17, -115.14)) == "NV"      # Las Vegas
    assert webapp._market_near((42.33, -83.05)) == "MI"       # Detroit
    assert webapp._market_near((25.77, -80.19)) == "FL"       # Miami
    webapp._geocode = lambda q, st="": (37.54, -77.44)        # a Virginia ZIP, market left at AZ
    r = client.get("/refer", params={"code": "73721", "q": "23220"})
    assert r.status_code == 200 and "Virginia" in r.text
    assert 'name="metro"' in r.text and 'type="hidden" name="metro"' not in r.text   # and correctable


def test_coinsurance_is_clamped_before_it_reaches_a_patient():
    """It arrives from a free-text box and gets multiplied by a price that is read aloud."""
    assert webapp._pct_in("20") == 20.0
    assert webapp._pct_in("200") == 100.0        # not 200% of the bill
    assert webapp._pct_in("-5") == 0.0
    assert webapp._pct_in("") is None and webapp._pct_in("abc") is None
    assert webapp._patient_cost(1000, 0, webapp._pct_in("200")) == 1000


def test_lowest_badge_is_inside_the_radius_and_never_a_known_error():
    """The flag came from the state-wide breakdown and was then radius-filtered away, so a
    radius search often highlighted nothing; and a row flagged 'likely error' could win it."""
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    _, rows, _, _, _, _, _ = webapp._near(webapp._svc("73721"), "AZ", "85015", "25")
    flagged = [h for h in rows if h.get("cheapest")]
    priced = [h for h in rows if h["price"] is not None and h.get("outlier") != "low"]
    assert len(flagged) == (1 if priced else 0)        # exactly one badge, and only if earnable
    if flagged:
        assert flagged[0]["price"] == min(h["price"] for h in priced)
        assert flagged[0].get("outlier") != "low"
    assert not (rows and rows[0].get("outlier") == "low"), "a likely data error must not rank #1"


def test_every_priced_row_can_be_pinned_and_phoned():
    """Freestanding rows carry id None, so geo.get(None) missed and the cheapest option -- which
    is usually a cash-priced freestanding site -- got no pin while still being counted."""
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    _, rows, _, _, _, _, _ = webapp._near(webapp._svc("73721"), "AZ", "85015", "50")
    nopin = [h["name"] for h in rows if h["lat"] is None]
    assert not nopin, f"in the table but not on the map: {nopin[:4]}"


def test_patient_column_is_not_gated_on_the_first_row():
    """The whole out-of-pocket column was gated on rows[0].patient, so if the nearest facility
    had no rate for the chosen plan it vanished for every facility that did."""
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    t = client.get("/map", params={"code": "73721", "q": "85015", "ded": "500", "coins": "20"}).text
    assert "rows[0]" not in t and "Patient pays" in t


def test_nav_dropdown_is_not_served_open():
    """An absolutely-positioned panel covering the search form was the first thing a user saw."""
    for path in ("/map", "/rates?code=73721", "/payers", "/bundles?episode=tka"):
        t = client.get(path).text
        assert "<details class=\"onav-menu\">" in t, f"{path} serves the menu open"
    assert "Escape" in client.get("/map").text          # and it can be dismissed


def test_inline_script_parses():
    if not shutil.which("node"):
        return
    js = re.findall(r"<script>(.*?)</script>", client.get("/").text, re.S)[-1]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    res = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    os.remove(f.name)
    assert res.returncode == 0, res.stderr[:400]


def test_no_facility_carries_another_facilitys_address():
    """contact.yaml was built by matching CMS records to the manifest, and for multi-campus
    systems the match fell through to a sibling: 19 facilities across 9 addresses ended up
    with a different hospital's street address. SUMMIT-SHOW-LOW carried Banner Casa Grande's,
    221 km away. Two campuses in one building may share an address; two that are a kilometre
    apart may not."""
    import math
    import yaml
    from collections import defaultdict

    def load(n):
        d = yaml.safe_load(open(f"config-ortho/{n}", encoding="utf-8")) or {}
        return d.get("hospitals", d)

    contact, geo = load("contact.yaml"), load("geo.yaml")

    def km(a, b):
        p1, p2 = math.radians(a[0]), math.radians(b[0])
        h = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
        return 2 * 6371.0 * math.asin(math.sqrt(h))

    by = defaultdict(list)
    for k, v in contact.items():
        if v.get("address"):
            by[(v["address"].strip().lower(), v.get("city", "").strip().lower())].append(k)
    bad = []
    for addr, ids in by.items():
        pts = [geo[i] for i in ids if geo.get(i)]
        if len(ids) > 1 and max((km(p, q) for p in pts for q in pts), default=0) > 1.0:
            bad.append(f"{addr[0]} shared by {ids}")
    assert not bad, "facilities share an address but are far apart: " + "; ".join(bad)


def test_map_inline_script_parses():
    if not shutil.which("node"):
        return
    webapp._geocode = lambda q, st="": (33.48, -112.07)
    t = client.get("/map", params={"code": "73721", "q": "85015"}).text
    js = re.findall(r"<script>(.*?)</script>", t, re.S)[-1]
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
