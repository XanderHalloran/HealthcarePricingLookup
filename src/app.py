"""Local web app: paste codes -> per-code pricing + negotiation talking points.

    uvicorn app:app --reload --app-dir src      (from repo root)
    # then open http://127.0.0.1:8000

No auth, no accounts (MVP). Medicare reference is loaded into an in-memory DuckDB
at startup from config/medicare.yaml; hospital rates are read from the Parquet
dataset produced by the ingest pipeline (run src/pipeline.py first to populate it).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from functools import lru_cache

import yaml
from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(__file__))
from medicare import build_reference_db, build_reference
from pricing import (analyze, load_templates, load_services, load_hospital_names,
                     load_quality, load_hip_knee, load_freestanding, load_metros, load_geo,
                     payer_matrix, get_medicare_all, get_medicare, lookup)
from analysis import detect_issues, build_letter, savings_summary
from assistant import ask, build_context
from eob import extract_eob
from payers import CANONICAL
import capture

# Friendly section headers for the shop picker, keyed by allowlist category.
CAT_LABELS = {
    "preventive": "Preventive screenings", "imaging": "Imaging / radiology",
    "procedure": "Procedures", "womens_health": "Women's health",
    "lab": "Labs", "er_em": "Emergency room", "office": "Office visits",
    # ortho profile categories
    "joint": "Joint replacement", "arthroscopy": "Arthroscopy & sports", "spine": "Spine",
    "fracture": "Fracture care", "hand": "Hand & upper extremity", "foot": "Foot & ankle",
    "injection": "Injections", "therapy": "Therapy, casts & splints", "dme": "Braces & DME",
    "inpatient": "Inpatient (MS-DRG)",
}

# Marquee, high-search procedures for the home-page footer link grid (SEO + nav).
POPULAR_CODES = ["73721", "74178", "70450", "77067", "45378", "27447",
                 "80053", "85027", "99285", "99213", "44970", "59400"]

# When the hospital price dataset was last (re)ingested — surfaced for provenance/trust.
# ponytail: hand-set on re-ingest; wire to the ingest timer only if it drifts in practice.
DATA_REFRESHED = "September 2026"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# One codebase, two sites: APP_CONFIG=config (healthcare.traqqit.com, consumer) or
# config-ortho (ortho.traqqit.com, MSK price intelligence). Each has its own data volume.
CFG = os.path.join(ROOT, os.environ.get("APP_CONFIG", "config"))
BASE_URL = os.environ.get("BASE_URL", "https://healthcare.traqqit.com").rstrip("/")
SCHEMA = os.path.join(ROOT, "src", "schema.sql")
RATES_DIR = os.path.join(ROOT, "data", "hospital_rates")
PAYER_DIR = os.path.join(ROOT, "data", "payer_rates")

def _load_brand():
    """Site identity from <CFG>/brand.yaml; the consumer config has none -> defaults."""
    p = os.path.join(CFG, "brand.yaml")
    b = {"profile": "consumer", "name": "Medical Price & Bill Coach", "short": "Bill Coach",
         "tagline": "", "audience": ""}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            b.update(yaml.safe_load(f) or {})
    return b


BRAND = _load_brand()
OWN_IDS = set((BRAND.get("own") or {}).get("ids") or [])      # our facilities (badged, /position)
PAYER_LIST = [p for p in CANONICAL if p not in set(BRAND.get("exclude_payers") or [])]   # ortho hides Medicaid
app = FastAPI(title=BRAND["name"])
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "src", "web", "static")),
          name="static")
templates = Jinja2Templates(directory=os.path.join(ROOT, "src", "web", "templates"))


def _load():
    con = build_reference_db(":memory:", SCHEMA)
    with open(os.path.join(CFG, "medicare.yaml"), encoding="utf-8") as f:
        med_cfg = yaml.safe_load(f)
    build_reference(con, ROOT, med_cfg)
    with open(os.path.join(CFG, "pricing.yaml"), encoding="utf-8") as f:
        multiplier = yaml.safe_load(f).get("target_multiplier", 2.0)
    services = load_services(os.path.join(CFG, "allowlist.yaml"))
    return {
        "con": con,
        "templates": load_templates(os.path.join(CFG, "templates.yaml")),
        "services": services,
        "descriptions": {(s["code"], s["type"]): s["desc"] for s in services},
        "hospital_names": load_hospital_names(os.path.join(CFG, "hospitals.yaml")),
        "hospital_stars": load_quality(os.path.join(CFG, "quality.yaml")),
        "hip_knee": load_hip_knee(os.path.join(CFG, "quality.yaml")),
        "metros": load_metros(os.path.join(CFG, "metros.yaml")),
        "geo": load_geo(os.path.join(CFG, "geo.yaml")),
        "freestanding": load_freestanding(os.path.join(CFG, "freestanding.yaml")),
        "multiplier": multiplier,
    }


STATE = _load()
# Curated popular list (desc + code) for the footer, in POPULAR_CODES order.
_by_code = {s["code"]: s for s in STATE["services"]}
STATE["popular"] = [_by_code[c] for c in POPULAR_CODES if c in _by_code] or \
    [s for s in STATE["services"] if s.get("slug")][:12]        # ortho: the slugged procedures
DEFAULT_STATE = "AZ"   # the original market; its statewide SEO pages keep the bare /procedure/ path

# Top shoppable procedures (allowlist entries with a `slug`) get /<state>/<metro>/<slug> SEO pages.
TOP_PROCS = [s for s in STATE["services"] if s.get("slug")]
SLUG_TO_CODE = {s["slug"]: s["code"] for s in TOP_PROCS}
CODE_TO_SLUG = {s["code"]: s["slug"] for s in TOP_PROCS}

# Markets = states with data (config/metros.yaml `states`). code -> {code,name,slug,n}.
MARKETS = {s["code"]: dict(s) for s in STATE["metros"]["states"]}
MARKET_BY_SLUG = {s["slug"]: s["code"] for s in MARKETS.values()}
HOSP_STATE = {}
with open(os.path.join(CFG, "hospitals.yaml"), encoding="utf-8") as _f:
    for _h in yaml.safe_load(_f)["hospitals"]:
        _st = str(_h["state"]).upper()
        HOSP_STATE[_h["id"]] = _st
        if _st in MARKETS:
            MARKETS[_st]["n"] = MARKETS[_st].get("n", 0) + 1


def _live_states():
    """Markets that actually have ingested data (their Parquet partition exists) — so a state
    can be configured ahead of its ingest and appear the moment its data lands, no restart."""
    return [s for s in MARKETS.values()
            if s["code"] == DEFAULT_STATE or os.path.isdir(os.path.join(RATES_DIR, f"state={s['code']}"))]


def _market(state):
    """Normalize a user/URL-supplied state to a live market code (else the default)."""
    st = (state or "").strip().upper()
    return st if st in {s["code"] for s in _live_states()} else DEFAULT_STATE


def _area(metro, state=""):
    """Resolve the ortho pages' single Area choice: a metro key, a state code ("All Virginia"),
    or nothing (-> `state` if live, else the default market). Returns (state, metro, metro_ids)."""
    metro = (metro or "").strip()
    if metro in STATE["metros"]["ids"]:
        return STATE["metros"]["state_of"][metro], metro, STATE["metros"]["ids"][metro]
    st = _market(metro or state)
    return st, (st if metro.upper() == st else ""), None


def _area_ctx(state, metro):
    """Template context shared by the analysis pages' Area selector."""
    return {"state": state, "metro": metro, "state_name": MARKETS[state]["name"],
            "state_list": _live_states(), "metro_list": STATE["metros"]["list"],
            "n_hosp": MARKETS[state].get("n", 0)}


def _state_path(state):
    """URL prefix of a state's statewide procedure pages ('' for the default market)."""
    return "" if state == DEFAULT_STATE else f"/{MARKETS[state]['slug']}"


def _money_in(s):
    """Parse a user-entered dollar amount ('$1,200' / '1200') to float, or None."""
    s = (s or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s) or None
    except ValueError:
        return None


def _ctx(rows, codes, state, mode, payer="", gfe=None, metro=""):
    metro = metro if metro in STATE["metros"]["ids"] else ""
    state = STATE["metros"]["state_of"][metro] if metro else _market(state)
    metros = [m for m in STATE["metros"]["list"] if m["state"] == state]
    return {"rows": rows, "codes": codes, "state": state, "mode": mode,
            "services": STATE["services"], "cat_labels": CAT_LABELS,
            "payer_list": PAYER_LIST, "payer": payer,
            "metro": metro, "metro_list": metros,
            "state_list": _live_states(), "state_name": MARKETS[state]["name"],
            "state_path": _state_path(state), "n_hosp": MARKETS[state].get("n", 0),
            "promoted": [m for m in metros if m["promote"]][:2],
            "capture_on": capture.configured(), "brand": BRAND, "base": BASE_URL, "own_ids": OWN_IDS,
            # every live metro + per-state counts, so the Area list switches client-side (no reload)
            "all_metros": [{"key": m["key"], "label": m["label"], "state": m["state"]}
                           for m in STATE["metros"]["list"] if m["state"] in {x["code"] for x in _live_states()}],
            "market_n": {x["code"]: x.get("n", 0) for x in _live_states()},
            "market_names": {x["code"]: x["name"] for x in _live_states()},
            "popular": STATE.get("popular", []), "data_refreshed": DATA_REFRESHED,
            "gfe": ("%.0f" % gfe) if gfe else "",
            "issues": detect_issues(rows, gfe) if rows else [],
            "savings": savings_summary(rows) if rows else None}


def _latlon(lat, lon):
    """Parse client-supplied coords to a (lat,lon) tuple, or None."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (la, lo) if (-90 <= la <= 90 and -180 <= lo <= 180) else None


def _analyze(codes, state, payer=None, metro=None, lat=None, lon=None):
    metro_ids = STATE["metros"]["ids"].get(metro) if metro else None
    if metro_ids is not None:                      # a metro implies its state
        state = STATE["metros"]["state_of"][metro]
    return analyze(STATE["con"], RATES_DIR, codes, (state or DEFAULT_STATE).strip(),
                   STATE["templates"], STATE["descriptions"], STATE["multiplier"],
                   STATE["hospital_names"], STATE["hospital_stars"], PAYER_DIR,
                   payer or None, STATE["freestanding"], metro or None, metro_ids,
                   _latlon(lat, lon), STATE["geo"], STATE["hip_knee"])


@app.get("/", response_class=HTMLResponse)
def index(request: Request, state: str = "", metro: str = ""):
    """Home. `?state=CA` (or `?metro=denver`) pre-selects a market; unknown -> default."""
    return templates.TemplateResponse(request, "index.html",
                                      _ctx(None, "", state, "shop", metro=metro))


@app.get("/card", response_class=HTMLResponse)
def card(request: Request, code: str, state: str = DEFAULT_STATE, payer: str = "",
         metro: str = "", lat: str = "", lon: str = ""):
    """Single price card for one code — fetched on demand by the shop tab so the
    page stays fast no matter how many services exist. `payer` ties prices to an insurer;
    `metro` restricts the range + facility list to one metro (apples-to-apples);
    `lat`/`lon` add a distance to each facility."""
    rows = [r for r in _analyze(code, state, payer, metro, lat, lon) if r["resolved"]]
    if not rows:
        return HTMLResponse('<div class="card">No data for that code in this market.</div>')
    return templates.TemplateResponse(request, "card.html", {"r": rows[0], "brand": BRAND, "own_ids": OWN_IDS})


@lru_cache(maxsize=4096)
def _zip_latlon(zip5):
    """ZIP -> (lat, lon) via the free, no-key zippopotam.us. Cached; raises on miss."""
    req = urllib.request.Request(f"https://api.zippopotam.us/us/{zip5}",
                                 headers={"User-Agent": "healthcare-price-tool/1.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        p = json.load(r)["places"][0]
    return float(p["latitude"]), float(p["longitude"])


@app.get("/geo")
def geo(zip: str = ""):
    """Resolve a US ZIP to coordinates for the distance sort. ponytail: leans on a free
    public API (cached) instead of bundling + maintaining a ZIP-centroid dataset."""
    z = "".join(ch for ch in (zip or "") if ch.isdigit())[:5]
    if len(z) != 5:
        return Response('{"error":"Enter a 5-digit ZIP"}', media_type="application/json", status_code=400)
    try:
        lat, lon = _zip_latlon(z)
        return Response(f'{{"lat":{lat},"lon":{lon},"zip":"{z}"}}', media_type="application/json")
    except Exception:
        return Response('{"error":"ZIP not found"}', media_type="application/json", status_code=404)


@app.post("/analyze", response_class=HTMLResponse)
def do_analyze(request: Request, codes: str = Form(""), shop_codes: list[str] = Form([]),
               state: str = Form(""), payer: str = Form(""), gfe: str = Form(""),
               metro: str = Form(""), lat: str = Form(""), lon: str = Form("")):
    # "Shop a price" tab posts selected service codes; "Analyze my bill" posts text.
    if shop_codes:
        codes, mode = "\n".join(shop_codes), "shop"
    else:
        mode = "bill"
    state = _market(state)
    rows = _analyze(codes, state, payer, metro, lat, lon)
    return templates.TemplateResponse(request, "index.html",
                                      _ctx(rows, codes, state, mode, payer, _money_in(gfe), metro))


def _metro_meta(key, state):
    """(city_label, canonical_path) for a metro key ('' = statewide for `state`)."""
    for m in STATE["metros"]["list"]:
        if m["key"] == key:
            return m["city"], f"/{key}/procedure/"
    return MARKETS[state]["name"], f"{_state_path(state)}/procedure/"


def _procedure_page(request, code, metro="", state=DEFAULT_STATE):
    """Shared builder for the SEO landing pages (statewide + per-metro)."""
    if metro:
        state = STATE["metros"]["state_of"][metro]
    rows = [r for r in _analyze(code, state, None, metro) if r["resolved"]]
    if not rows:
        return HTMLResponse("Unknown procedure code.", status_code=404)
    r = rows[0]
    d = r["result"]
    desc = d.get("description") or code
    city, path = _metro_meta(metro, state)
    n = d.get("n_hospitals") or 0
    low, high = d.get("neg_low"), d.get("neg_high")
    price_bit = (f"from ${low:,.0f}" if low is not None else "prices")
    meta = (f"Compare {desc} (CPT {code}) prices across {n} hospitals in {city} — {price_bit}. "
            f"See the fair cash price, each hospital's rate, the Medicare benchmark, and how to "
            f"negotiate. Free, no signup.")
    # Internal linking: this state's other metros + "All <State>", then every other state.
    siblings = [{"label": m["label"], "url": f"{BASE_URL}/{m['key']}/procedure/{code}"}
                for m in STATE["metros"]["list"] if m["key"] != metro and m["state"] == state]
    if metro:
        siblings.insert(0, {"label": f"All {MARKETS[state]['name']}",
                            "url": f"{BASE_URL}{_state_path(state)}/procedure/{code}"})
    siblings += [{"label": f"All {s['name']}", "url": f"{BASE_URL}{_state_path(s['code'])}/procedure/{code}"}
                 for s in _live_states() if s["code"] != state]
    related = [{"desc": s["desc"], "code": s["code"],
                "url": f"{BASE_URL}{path}{s['code']}"}
               for s in STATE["services"] if s["code"] != code][:8]
    canonical = f"{BASE_URL}{path}{code}"
    if metro and code in CODE_TO_SLUG:          # top procedures: the readable URL is canonical
        canonical = f"{BASE_URL}/{MARKETS[state]['slug']}/{metro}/{CODE_TO_SLUG[code]}"
        related = [{"desc": s["desc"], "code": s["code"],
                    "url": f"{BASE_URL}/{MARKETS[state]['slug']}/{metro}/{s['slug']}"}
                   for s in TOP_PROCS if s["code"] != code]
    faqs = _build_faqs(desc, code, city, d)
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "MedicalProcedure", "name": desc, "url": canonical,
         "code": {"@type": "MedicalCode", "code": code, "codingSystem": "CPT"}},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": BASE_URL},
            {"@type": "ListItem", "position": 2, "name": city, "item": f"{BASE_URL}{path}".rstrip("/")},
            {"@type": "ListItem", "position": 3, "name": f"{desc} (CPT {code})", "item": canonical}]},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": f["q"],
             "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faqs]},
    ]}
    return templates.TemplateResponse(request, "procedure.html", {
        "r": r, "city": city, "metro": metro, "desc": desc, "base": BASE_URL, "brand": BRAND,
        "n_hosp": MARKETS[state].get("n", 0), "nav": "compare", "own_ids": OWN_IDS,
        "canonical": canonical, "faqs": faqs, "ld": ld, "data_refreshed": DATA_REFRESHED,
        "title": f"How much does {desc} (CPT {code}) cost in {city}? | Price & Negotiation",
        "meta_desc": meta, "siblings": siblings, "related": related,
    })


def _build_faqs(desc, code, city, d):
    """Visible + schema FAQ, grounded in this page's numbers."""
    def m(v):
        return f"${v:,.0f}" if v is not None else None
    low, med, high = d.get("neg_low"), d.get("neg_median"), d.get("neg_high")
    mc, n = d.get("medicare_rate"), d.get("n_hospitals") or 0
    faqs = []
    if med is not None:
        faqs.append({"q": f"How much does {desc} cost in {city}?",
                     "a": (f"A fair self-pay price for {desc} (CPT {code}) in {city} is around "
                           f"{m(med)}, with hospital prices ranging from {m(low)} to {m(high)} "
                           f"across {n} facilities. These are estimates from hospital-published "
                           f"prices, not a guarantee.")})
    else:
        faqs.append({"q": f"How much does {desc} cost in {city}?",
                     "a": (f"Hospital prices for {desc} (CPT {code}) in {city} vary widely. "
                           f"Compare each facility's published rate above and use the Medicare "
                           f"benchmark as your negotiation floor.")})
    if mc is not None:
        faqs.append({"q": f"What is the Medicare rate for CPT {code}?",
                     "a": (f"Medicare pays about {m(mc)} for {desc} (CPT {code}). That's the "
                           f"negotiation floor — a fair cash-pay target is roughly 1.5–2× it, "
                           f"well below typical gross charges.")})
    faqs.append({"q": f"Can I negotiate the price of {desc}?",
                 "a": ("Yes. Under the federal Hospital Price Transparency rule hospitals must "
                       "publish these rates, so you can ask to pay the cash or negotiated rate "
                       "instead of the gross charge. Paste your bill on the home page for a "
                       "ready-to-send appeal letter and the exact numbers to cite.")})
    return faqs


@app.get("/procedure/{code}", response_class=HTMLResponse)
def procedure(request: Request, code: str):
    """Statewide SEO landing page for the default market: '...cost in Arizona?'"""
    return _procedure_page(request, code, "")


@app.get("/{area}/procedure/{code}", response_class=HTMLResponse)
def procedure_area(request: Request, area: str, code: str):
    """Per-metro ('/tucson/procedure/73721' → '...cost in Tucson?') or per-state
    ('/california/procedure/73721') SEO landing page — the ad-friendly long-tail."""
    if area in MARKET_BY_SLUG:
        if MARKET_BY_SLUG[area] != _market(MARKET_BY_SLUG[area]):
            return _coming_soon(MARKET_BY_SLUG[area])
        return _procedure_page(request, code, "", MARKET_BY_SLUG[area])
    if area not in STATE["metros"]["ids"]:
        return HTMLResponse("Unknown area.", status_code=404)
    if STATE["metros"]["state_of"][area] != _market(STATE["metros"]["state_of"][area]):
        return _coming_soon(STATE["metros"]["state_of"][area])
    return _procedure_page(request, code, area)


def _coming_soon(state):
    """Honest empty state for a configured-but-not-ingested market. 404 so it isn't indexed."""
    name = MARKETS[state]["name"]
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{name} hospital prices - coming soon</title>"
        f"<main style='font-family:system-ui;max-width:640px;margin:60px auto;padding:0 20px'>"
        f"<h1>{name} prices are coming soon</h1><p>We haven't loaded {name} hospitals' published "
        f"price files yet, and we won't show numbers we don't have. "
        f"<a href='/'>See the states that are live</a>.</p></main>", status_code=404)


@app.get("/{state_slug}/{metro}/{proc_slug}", response_class=HTMLResponse)
def procedure_slug(request: Request, state_slug: str, metro: str, proc_slug: str):
    """Readable SEO page for a top procedure: /arizona/phoenix/mri-brain.
    Registered AFTER /{area}/procedure/{code} so that pattern keeps winning."""
    state = MARKET_BY_SLUG.get(state_slug)
    if not state or STATE["metros"]["state_of"].get(metro) != state or proc_slug not in SLUG_TO_CODE:
        return HTMLResponse("Unknown page.", status_code=404)
    if state != _market(state):
        return _coming_soon(state)
    return _procedure_page(request, SLUG_TO_CODE[proc_slug], metro)


# ---------- Ortho profile: internal intelligence pages (basic-auth'd at the edge) ----------

def _groups():
    """Allowlist grouped by category, in allowlist order — for the procedure pickers."""
    out = {}
    for s in STATE["services"]:
        out.setdefault(s["category"], []).append(s)
    return list(out.items())


def _svc(code):
    return _by_code.get((code or "").strip().upper()) or STATE["services"][0]


def _col_label(col):
    return {"cash": "Cash / self-pay", "median": "Negotiated median"}.get(col, col)


@app.get("/rates", response_class=HTMLResponse)
def rates(request: Request, code: str = "", metro: str = "", mine: str = ""):
    """Payer-rate intelligence: facility × payer contracted rates for one code, with market
    percentiles and where a given rate sits. Feature 2 of the ortho brief."""
    svc = _svc(code)
    state, metro, metro_ids = _area(metro)
    m = payer_matrix(STATE["con"], RATES_DIR, PAYER_DIR, svc["code"], state,
                     STATE["hospital_names"], metro_ids, PAYER_LIST)
    for r in m["rows"]:
        r["hip_knee"] = STATE["hip_knee"].get(r["id"])
    cols = ["cash", "median"] + m["payers"]
    med_rate, med_src = get_medicare(STATE["con"], svc["code"], svc["type"])
    my = _money_in(mine)
    pct = {}
    if my:
        for col in cols:
            vals = [(r["rates"].get(col) if col in m["payers"] else r.get(col)) for r in m["rows"]]
            vals = [v for v in vals if v is not None]
            if vals:
                pct[col] = round(100 * sum(1 for v in vals if v < my) / len(vals))
    return templates.TemplateResponse(request, "rates.html", {
        "svc": svc, "m": m, "cols": cols, "col_label": _col_label, "groups": _groups(),
        "cat_labels": CAT_LABELS, **_area_ctx(state, metro),
        "metro_label": next((x["label"] for x in STATE["metros"]["list"] if x["key"] == metro), ""),
        "medicare": med_rate, "medicare_src": med_src, "mine": my, "mine_raw": mine, "pct": pct,
        "brand": BRAND, "nav": "rates", "own_ids": OWN_IDS,
        "data_refreshed": DATA_REFRESHED})


@app.get("/rates.csv")
def rates_csv(code: str = "", metro: str = ""):
    """The same matrix as CSV — contracting teams live in spreadsheets."""
    import csv
    import io
    svc = _svc(code)
    state, metro, metro_ids = _area(metro)
    m = payer_matrix(STATE["con"], RATES_DIR, PAYER_DIR, svc["code"], state,
                     STATE["hospital_names"], metro_ids, PAYER_LIST)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["facility_id", "facility", "cash", "negotiated_median"] + m["payers"])
    for r in m["rows"]:
        w.writerow([r["id"], r["name"], r["cash"], r["median"]] + [r["rates"].get(p) for p in m["payers"]])
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f"attachment; filename=rates_{svc['code']}_{metro or state}.csv"})


@app.get("/site-of-care", response_class=HTMLResponse)
def site_of_care(request: Request, code: str = "", metro: str = ""):
    """Hospital outpatient vs ASC for one code: Medicare's OPPS-vs-ASC differential, every
    hospital's price, every ASC with a published price, and the per-case shift savings."""
    svc = _svc(code)
    state, metro, metro_ids = _area(metro)
    d = lookup(STATE["con"], RATES_DIR, svc["code"], svc["type"], state, svc["desc"],
               STATE["multiplier"], STATE["hospital_names"], STATE["hospital_stars"], PAYER_DIR,
               None, STATE["freestanding"], metro or None, metro_ids, None, STATE["geo"], STATE["hip_knee"])
    hosp = [h for h in d["hospitals"] if h["type"] == "hospital" and h["price"] is not None]
    asc = [h for h in d["hospitals"] if h["type"] != "hospital" and h["price"] is not None]
    import statistics
    # Shift math uses facility-type sites only: MDsave "bundles" include surgeon/anesthesia fees,
    # so they are not comparable to a facility rate (they stay in the table, labelled).
    sites = [a["price"] for a in asc if a["type"] in ("asc", "imaging", "office")]
    asc_median = statistics.median(sites) if sites else None
    med = get_medicare_all(STATE["con"], svc["code"], svc["type"])
    # No published site price? Give the business reader an explicit ESTIMATE: the hospital typical
    # price scaled by Medicare's own ASC/OPPS ratio, i.e. "an ASC contracted at the market multiple".
    implied = (d["neg_median"] * med["ASC"] / med["OPPS"]
               if (asc_median is None and d.get("neg_median") and med.get("ASC") and med.get("OPPS")) else None)
    published_codes = sorted({c for c, lst in STATE["freestanding"].items()
                              if any(f["type"] in ("asc", "imaging", "office") and f.get("state", "AZ") == state for f in lst)})
    return templates.TemplateResponse(request, "site.html", {
        "svc": svc, "d": d, "hosp": hosp, "asc": asc, "asc_median": asc_median, "n_sites": len(sites),
        "implied": implied, "published_codes": published_codes, "med": med,
        "groups": _groups(), "cat_labels": CAT_LABELS, **_area_ctx(state, metro), "own_ids": OWN_IDS,
        "brand": BRAND, "nav": "site", "data_refreshed": DATA_REFRESHED})


@app.get("/position", response_class=HTMLResponse)
def position(request: Request, facility: str = "", metro: str = ""):
    """Our facility vs the market: every tracked code we publish a price for, our price beside
    the competitors' median, Medicare multiples, percentile, and payer-by-payer pairs."""
    from ingest import rates_source
    from pricing import _has_parquet
    own_list = [{"id": i, "name": STATE["hospital_names"].get(i, i)} for i in sorted(OWN_IDS)]
    if not own_list:
        return HTMLResponse("No own facilities configured (brand.yaml `own.ids`).", status_code=404)
    fac_id = facility if facility in OWN_IDS else own_list[0]["id"]
    state, metro, metro_ids = _area(metro, HOSP_STATE.get(fac_id, DEFAULT_STATE))
    codes = []
    if _has_parquet(RATES_DIR):
        codes = [c for (c,) in STATE["con"].execute(
            f"SELECT DISTINCT code FROM {rates_source(RATES_DIR)} WHERE hospital_id=? AND state=?",
            [fac_id, state]).fetchall()]
    rows, payers_seen = [], set()
    for svc in STATE["services"]:
        if svc["code"] not in codes:
            continue
        m = payer_matrix(STATE["con"], RATES_DIR, PAYER_DIR, svc["code"], state,
                         STATE["hospital_names"], (metro_ids | {fac_id}) if metro_ids is not None else None, PAYER_LIST)
        mine = next((r for r in m["rows"] if r["id"] == fac_id), None)
        if not mine:
            continue
        # Basis = NEGOTIATED median (the contract-relevant number). Published "cash" often equals
        # the gross charge (CORE's file does exactly that), which would rank us at the top for
        # the wrong reason; cash is shown in its own column instead.
        ours = mine["median"] if mine["median"] is not None else mine["cash"]
        if ours is None:
            continue
        others = [r for r in m["rows"] if r["id"] != fac_id]
        prices = [(r["median"] if r["median"] is not None else r["cash"]) for r in others]
        prices = [p for p in prices if p is not None]
        import statistics
        mkt_median = statistics.median(prices) if prices else None
        pct = round(100 * sum(1 for p in prices if p < ours) / len(prices)) if prices else None
        med_rate, _ = get_medicare(STATE["con"], svc["code"], svc["type"])
        pay = {}
        for p in m["payers"]:
            o = mine["rates"].get(p)
            vals = [r["rates"][p] for r in others if p in r["rates"]]
            if o is not None or vals:
                pay[p] = {"ours": o, "mkt": statistics.median(vals) if vals else None, "n": len(vals)}
                if o is not None:
                    payers_seen.add(p)
        rows.append({"code": svc["code"], "type": svc["type"], "desc": svc["desc"], "ours": ours, "ours_cash": mine["cash"],
                     "mkt_median": mkt_median, "n": len(prices), "medicare": med_rate, "pct": pct, "payers": pay})
    pcts = [r["pct"] for r in rows if r["pct"] is not None]
    import statistics
    payers = [p for p in PAYER_LIST if p in payers_seen]
    return templates.TemplateResponse(request, "position.html", {
        "brand": BRAND, "nav": "position", **_area_ctx(state, metro),
        "own_list": own_list, "fac_id": fac_id, "fac_name": STATE["hospital_names"].get(fac_id, fac_id),
        "metro": metro, "metro_list": STATE["metros"]["list"],
        "metro_label": next((x["label"] for x in STATE["metros"]["list"] if x["key"] == metro), ""),
        "rows": rows, "payers": payers, "med_pct": round(statistics.median(pcts)) if pcts else None,
        "n_below": sum(1 for r in rows if r["mkt_median"] and r["ours"] < r["mkt_median"]),
        "n_above": sum(1 for r in rows if r["mkt_median"] and r["ours"] >= r["mkt_median"]),
        "file_note": (f"{STATE['hospital_names'].get(fac_id, fac_id)} publishes prices for {len(rows)} of the "
                      f"{len(STATE['services'])} tracked codes; the rest are not in its machine-readable file."),
        "fac_note": ((BRAND.get("own") or {}).get("notes") or {}).get(fac_id),
        "data_refreshed": DATA_REFRESHED})


# ---------- Payer scorecard: every payer x every code, one parquet scan, cached per refresh ----------

@lru_cache(maxsize=16)
def _payer_table(state, metro, stamp):
    """{ (code, payer): {'median': m, 'n': n}, ('own', code, payer): rate } over the market."""
    from ingest import rates_source
    from pricing import _has_parquet
    import statistics
    out = {}
    if not _has_parquet(PAYER_DIR):
        return out
    metro_ids = STATE["metros"]["ids"].get(metro) if metro else None
    acc = {}
    for hid, code, payer, rate in STATE["con"].execute(
            f"""SELECT hospital_id, code, payer, TRY_CAST(negotiated_rate AS DOUBLE)
                FROM {rates_source(PAYER_DIR)} WHERE state=?""", [state]).fetchall():
        if rate is None or payer not in PAYER_LIST:
            continue
        if hid in OWN_IDS:
            out[("own", code, payer)] = rate
        if metro_ids is not None and hid not in metro_ids:
            continue
        acc.setdefault((code, payer), []).append(rate)
    floors = {}
    for code in {c for c, _ in acc}:
        svc = _by_code.get(code)
        med, _ = get_medicare(STATE["con"], code, svc["type"]) if svc else (None, None)
        floors[code] = med * 0.25 if med else 0
    for (code, payer), vals in acc.items():
        # Same plausibility floor as the typical price: a $10.62 "rate" for a knee replacement is a
        # professional or per-unit component in that file, not a facility contract (Michigan's DMC,
        # Trinity and Henry Ford files all do this for surgical CPTs while their DRG rows look normal).
        keep = [v for v in vals if v >= floors[code]]
        if keep:
            out[(code, payer)] = {"median": statistics.median(keep), "n": len(keep), "dropped": len(vals) - len(keep)}
    return out


def _payer_rows(state, metro):
    stamp = os.path.getmtime(PAYER_DIR) if os.path.isdir(PAYER_DIR) else 0
    tbl = _payer_table(state, metro, stamp)
    rows = []
    for svc in STATE["services"]:
        med, _ = get_medicare(STATE["con"], svc["code"], svc["type"])
        cells = {p: tbl[(svc["code"], p)] for p in PAYER_LIST if (svc["code"], p) in tbl}
        if cells:
            rows.append({"code": svc["code"], "type": svc["type"], "desc": svc["desc"], "medicare": med,
                         "cells": cells, "owns": {p: tbl.get(("own", svc["code"], p)) for p in PAYER_LIST}})
    return rows


MIN_FAC = 3   # facilities a (code, payer) cell needs before it counts toward the payer ranking


@app.get("/payers", response_class=HTMLResponse)
def payer_scorecard(request: Request, payer: str = "", metro: str = ""):
    """Payer scorecard: each insurer's market median as a multiple of Medicare, per procedure."""
    import statistics
    state, metro, _ = _area(metro)
    sel = payer if payer in PAYER_LIST else ""
    rows = _payer_rows(state, metro)
    payers = [p for p in PAYER_LIST if any(p in r["cells"] for r in rows)]
    ranking = []
    for p in payers:
        # ponytail: a payer published by 2-4 files (e.g. UVA's ~$1.3k "Tricare" surgery rows) can drive a
        # statewide multiple; only cells backed by >= MIN_FAC facilities count toward the ranking.
        mults = [r["cells"][p]["median"] / r["medicare"] for r in rows
                 if p in r["cells"] and r["medicare"] and r["cells"][p]["n"] >= MIN_FAC]
        thin = sum(1 for r in rows if p in r["cells"] and r["cells"][p]["n"] < MIN_FAC)
        if mults:
            ranking.append({"payer": p, "mult": round(statistics.median(mults), 2), "codes": len(mults), "thin": thin})
    ranking.sort(key=lambda x: x["mult"])
    for r in rows:
        r["own"] = r["owns"].get(sel) if sel else None
    return templates.TemplateResponse(request, "payers.html", {
        "brand": BRAND, "nav": "payers", **_area_ctx(state, metro),
        "rows": rows, "payers": payers, "sel": sel, "ranking": ranking, "min_fac": MIN_FAC, "own_label": (BRAND.get("own") or {}).get("label") if OWN_IDS else "",
        "data_refreshed": DATA_REFRESHED})


@app.get("/payers.csv")
def payer_scorecard_csv(metro: str = ""):
    import csv
    import io
    state, metro, _ = _area(metro)
    rows = _payer_rows(state, metro)
    payers = [p for p in PAYER_LIST if any(p in r["cells"] for r in rows)]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "type", "description", "medicare"] + [f"{p} median" for p in payers] + [f"{p} n" for p in payers])
    for r in rows:
        w.writerow([r["code"], r["type"], r["desc"], r["medicare"]]
                   + [r["cells"][p]["median"] if p in r["cells"] else "" for p in payers]
                   + [r["cells"][p]["n"] if p in r["cells"] else "" for p in payers])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=payer_scorecard_{metro or state}.csv"})


# ---------- Episode (bundle) pricing ----------

def _bundles_cfg():
    p = os.path.join(CFG, "bundles.yaml")
    if not os.path.exists(p):
        return {"episodes": [], "commercial_multiplier": 1.4, "anesthesia_cf": 20.3178}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


BUNDLES = _bundles_cfg()


@app.get("/bundles", response_class=HTMLResponse)
def bundles(request: Request, episode: str = "", metro: str = "", mine: str = ""):
    """Episode pricing: facility + surgeon + anesthesia + pre-op + rehab, as Medicare / market
    hospital-based / ASC-based (/ own-facility-based) episodes, with a 'your bundle' gap."""
    import statistics
    eps = BUNDLES.get("episodes") or []
    if not eps:
        return HTMLResponse("No episodes configured (config-ortho/bundles.yaml).", status_code=404)
    ep = next((e for e in eps if e["key"] == episode), eps[0])
    state, metro, metro_ids = _area(metro)
    mult = float(BUNDLES.get("commercial_multiplier", 1.4))
    acf = float(BUNDLES.get("anesthesia_cf", 20.3178))
    own_label = (BRAND.get("own") or {}).get("label") if OWN_IDS else ""

    def svc_of(code):
        return _by_code.get(code) or {"code": code, "type": "CPT", "desc": code}

    # --- facility component ---
    fcode = ep["facility"]
    fs = svc_of(fcode)
    d = lookup(STATE["con"], RATES_DIR, fcode, fs["type"], state, fs["desc"], STATE["multiplier"],
               STATE["hospital_names"], STATE["hospital_stars"], PAYER_DIR, None, STATE["freestanding"],
               metro or None, metro_ids, None, STATE["geo"], STATE["hip_knee"])
    med_all = get_medicare_all(STATE["con"], fcode, fs["type"])
    hosp_typ = d.get("neg_median")
    sites = [h["price"] for h in d["hospitals"] if h["type"] in ("asc", "imaging", "office") and h["price"] is not None]
    asc_pub = statistics.median(sites) if sites else None
    asc_est = (hosp_typ * med_all["ASC"] / med_all["OPPS"]
               if (asc_pub is None and hosp_typ and med_all.get("ASC") and med_all.get("OPPS")) else None)
    own_fac = None
    if OWN_IDS:
        m = payer_matrix(STATE["con"], RATES_DIR, PAYER_DIR, fcode, state, STATE["hospital_names"], None, PAYER_LIST)
        mine_row = next((r for r in m["rows"] if r["id"] in OWN_IDS), None)
        if mine_row:
            own_fac = mine_row["median"] if mine_row["median"] is not None else mine_row["cash"]
    cols = [{"key": "medicare", "label": "All-Medicare episode", "short": "Medicare", "note": "fee schedules only"},
            {"key": "hospital", "label": "Market hospital-based", "short": "Hospital-based", "note": f"{d.get('n_hospitals') or 0} facilities' typical price"},
            {"key": "asc", "label": "ASC-based", "short": "ASC-based", "note": "published site price" if asc_pub else "implied at market multiple"}]
    if own_fac:
        cols.append({"key": "own", "label": f"{own_label} facility-based", "short": own_label, "note": "our negotiated median"})
    comps = [{"label": f"Facility: {fs['desc']}", "code": fs["code"], "basis": "MRF typical / ASC / Medicare OPPS+ASC",
              "vals": {"medicare": med_all.get("OPPS"), "hospital": hosp_typ, "asc": asc_pub or asc_est, "own": own_fac},
              "est": {"asc": asc_pub is None and asc_est is not None}}]

    def pro_rate(code):
        rates = get_medicare_all(STATE["con"], code, svc_of(code)["type"])
        return rates.get("PFS")

    # --- surgeon ---
    pr = pro_rate(ep["pro"])
    if pr:
        comps.append({"label": f"Surgeon: {svc_of(ep['pro'])['desc']}", "code": ep["pro"], "basis": f"PFS × {mult} (commercial)",
                      "vals": {"medicare": pr, "hospital": pr * mult, "asc": pr * mult, "own": pr * mult}, "est": {"hospital": True, "asc": True, "own": True}})
    # --- anesthesia ---
    an = ep.get("anesthesia")
    if an:
        units = float(an["base_units"]) + float(an["minutes"]) / 15.0
        a_med = units * acf
        comps.append({"label": f"Anesthesia ({an['code']}, {units:g} units)", "code": an["code"], "basis": f"units × ${acf} × {mult}",
                      "vals": {"medicare": a_med, "hospital": a_med * mult, "asc": a_med * mult, "own": a_med * mult}, "est": {"medicare": True, "hospital": True, "asc": True, "own": True}})
    # --- visits / rehab ---
    for v in ep.get("visits") or []:
        r = pro_rate(v["code"])
        if r:
            comps.append({"label": v["label"], "code": v["code"], "basis": f"PFS × {v['n']} × {mult}",
                          "vals": {"medicare": r * v["n"], "hospital": r * v["n"] * mult, "asc": r * v["n"] * mult, "own": r * v["n"] * mult}, "est": {"hospital": True, "asc": True, "own": True}})
    for col in cols:
        vals = [c["vals"].get(col["key"]) for c in comps]
        col["total"] = sum(vals) if all(v is not None for v in vals) else None
    # --- inpatient alternative ---
    drg_row = None
    if ep.get("drg"):
        dm = payer_matrix(STATE["con"], RATES_DIR, PAYER_DIR, ep["drg"], state, STATE["hospital_names"], metro_ids, PAYER_LIST)
        others = [(r["median"] if r["median"] is not None else r["cash"]) for r in dm["rows"] if r["id"] not in OWN_IDS]
        others = [x for x in others if x is not None]
        own_drg = next(((r["median"] if r["median"] is not None else r["cash"]) for r in dm["rows"] if r["id"] in OWN_IDS), None)
        ipps, _ = get_medicare(STATE["con"], ep["drg"], "DRG")
        drg_row = {"market": statistics.median(others) if others else None, "n": len(others), "ipps": ipps, "own": own_drg}
    my = _money_in(mine)
    return templates.TemplateResponse(request, "bundles.html", {
        "brand": BRAND, "nav": "bundles", **_area_ctx(state, metro),
        "ep": ep, "episodes": eps, "cfg": BUNDLES, "cols": cols, "comps": comps, "drg_row": drg_row,
        "mine": my, "mine_raw": mine,
        "own_label": own_label, "own_col": bool(own_fac), "data_refreshed": DATA_REFRESHED})


@lru_cache(maxsize=1)
def _coverage(stamp):
    """Live coverage numbers for the methodology page (cached per data refresh stamp)."""
    from ingest import rates_source
    from pricing import _has_parquet
    cov = {"hospitals": 0, "codes": len(STATE["services"]), "rows": 0, "payers": len(PAYER_LIST), "states": []}
    if _has_parquet(RATES_DIR):
        for st, n, rows in STATE["con"].execute(
                f"SELECT state, count(distinct hospital_id), count(*) FROM {rates_source(RATES_DIR)} GROUP BY 1 ORDER BY 1").fetchall():
            if st in MARKETS:
                cov["states"].append({"code": st, "name": MARKETS[st]["name"], "n": n})
            cov["hospitals"] += n
            cov["rows"] += rows
    return cov


@lru_cache(maxsize=2048)
def _geocode(q, state_name=""):
    """Address or ZIP -> (lat, lon). ZIPs go to zippopotam; anything else to Nominatim (OSM),
    biased to the chosen market's state. Raises on miss."""
    z = "".join(ch for ch in q if ch.isdigit())
    if len(z) == 5 and len(q.strip()) <= 10:
        return _zip_latlon(z)
    if state_name and state_name.lower() not in q.lower():
        q = f"{q}, {state_name}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "countrycodes": "us"})
    req = urllib.request.Request(url, headers={"User-Agent": "ortho-price-tool/1.0 (ortho.traqqit.com)"})
    with urllib.request.urlopen(req, timeout=8) as r:
        hit = json.load(r)[0]
    return float(hit["lat"]), float(hit["lon"])


@app.get("/map", response_class=HTMLResponse)
def near_map(request: Request, code: str = "", q: str = "", radius: str = "25", sort: str = "price", metro: str = ""):
    """Facilities within a radius of an address or ZIP for one procedure, on a map."""
    svc = _svc(code)
    state, metro, _ = _area(metro)
    try:
        rad = min(max(int(radius), 5), 200)
    except ValueError:
        rad = 25
    center = err = None
    if q.strip():
        try:
            center = _geocode(q.strip(), MARKETS[state]["name"])
        except Exception:
            err = "Couldn't find that address or ZIP. Try a ZIP code or 'street, city'."
    rows, med_rate = [], None
    if center:
        d = _analyze(svc["code"], state, None, None, center[0], center[1])
        hosp = d[0]["result"]["hospitals"] if d and d[0]["resolved"] else []
        rows = [h for h in hosp if h.get("distance") is not None and h["distance"] <= rad]
        for h in rows:
            h["lat"], h["lon"] = STATE["geo"].get(h["id"]) or (None, None)
            h["hip_knee"] = STATE["hip_knee"].get(h["id"]) if h["id"] else None
        rows.sort(key=lambda h: (h["distance"] if sort == "dist" else (h["price"] if h["price"] is not None else 1e12)))
        med_rate, _ = get_medicare(STATE["con"], svc["code"], svc["type"])
    return templates.TemplateResponse(request, "map.html", {
        "svc": svc, "q": q, "radius": rad, "sort": sort, "center": center, "err": err, "rows": rows,
        "medicare": med_rate, "radii": [5, 10, 25, 50, 100], "groups": _groups(), "cat_labels": CAT_LABELS,
        "brand": BRAND, "nav": "map", **_area_ctx(state, metro), "own_ids": OWN_IDS,
        "data_refreshed": DATA_REFRESHED})


@app.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request):
    """Where every number comes from, with the data-flow diagram. Same page on both profiles."""
    with open(os.path.join(CFG, "medicare.yaml"), encoding="utf-8") as f:
        med = yaml.safe_load(f) or {}
    med_sources = {s for (s,) in STATE["con"].execute("SELECT DISTINCT source FROM medicare_rates").fetchall()}
    stamp = os.path.getmtime(RATES_DIR) if os.path.isdir(RATES_DIR) else 0
    return templates.TemplateResponse(request, "methodology.html", {
        "brand": BRAND, "base": BASE_URL, "nav": "method", "cov": _coverage(stamp),
        "state_name": MARKETS[DEFAULT_STATE]["name"], "n_hosp": MARKETS[DEFAULT_STATE].get("n", 0),
        "med_year": med.get("year", 2025), "pfs_cf": med.get("pfs_conversion_factor", ""),
        "med_sources": med_sources, "has_hk": bool(STATE["hip_knee"]), "data_refreshed": DATA_REFRESHED,
        "groups": _groups(), "cat_labels": CAT_LABELS, "state_slug": MARKETS[DEFAULT_STATE]["slug"],
        "first_metro": next((m["key"] for m in STATE["metros"]["list"] if m["state"] == DEFAULT_STATE), "")})


@app.get("/sitemap.xml")
def sitemap():
    # Every state statewide for every code, plus each state's promoted (dense/ad-worthy)
    # metros. Thin metro pages still resolve + are crawlable via on-page links; we just
    # don't promote them in the sitemap. ponytail: no per-combo data query.
    urls = [f"{BASE_URL}/", f"{BASE_URL}/methodology"]
    live = {s["code"] for s in _live_states()}
    promoted = [m["key"] for m in STATE["metros"]["list"] if m["promote"] and m["state"] in live]
    for s in STATE["services"]:
        for st in live:
            urls.append(f"{BASE_URL}{_state_path(st)}/procedure/{s['code']}")
        for mk in promoted:
            urls.append(f"{BASE_URL}/{mk}/procedure/{s['code']}")
    # Top procedures x every live metro, at their readable canonical URL.
    for m in STATE["metros"]["list"]:
        if m["state"] in live:
            for s in TOP_PROCS:
                urls.append(f"{BASE_URL}/{MARKETS[m['state']]['slug']}/{m['key']}/{s['slug']}")
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")
    return Response(body, media_type="application/xml")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n")


@app.post("/eob", response_class=PlainTextResponse)
def eob(file: UploadFile = File(...)):
    """OCR a bill/EOB photo or PDF into code lines for the bill textarea.
    Sync route → FastAPI runs it in a threadpool, so the blocking API call to Claude
    doesn't stall the event loop. ponytail: 12 MB cap; add rate-limiting if the public
    endpoint gets abused (each call hits the paid vision API)."""
    data = file.file.read()
    if len(data) > 12_000_000:
        return PlainTextResponse("File too large (max ~12 MB). Try a clearer single-page photo.")
    text = extract_eob(data, file.content_type or "image/jpeg")
    return PlainTextResponse(text or
        "Bill-photo reading isn't enabled on this server yet (needs an API key), or no "
        "billing codes were found — type the codes in manually for now.")


@app.post("/ask", response_class=PlainTextResponse)
def ask_endpoint(question: str = Form(""), codes: str = Form(""), state: str = Form(""),
                 payer: str = Form(""), gfe: str = Form(""), metro: str = Form("")):
    """AI coach: answer a free-text question grounded in the analyzed bill (Claude Fable 5).
    Sync route → threadpool, so the blocking API call doesn't stall the loop. Gated on the
    key; ponytail: add a per-IP cap before promoting — each call hits the paid API."""
    q = (question or "").strip()
    if not q:
        return PlainTextResponse("Type a question about your bill first.")
    if len(q) > 1000:
        return PlainTextResponse("Question too long — keep it under ~1000 characters.")
    state = _market(state)
    rows = _analyze(codes, state, payer, metro)
    ctx = build_context(rows, detect_issues(rows, _money_in(gfe)), state)
    answer = ask(q, ctx)
    return PlainTextResponse(answer or
        "The AI coach isn't enabled on this server yet (needs an API key). The talking "
        "points and the negotiation letter above work without it.")


@app.post("/email", response_class=PlainTextResponse)
def email_me(request: Request, email: str = Form(""), kind: str = Form("letter"), codes: str = Form(""),
             state: str = Form(""), metro: str = Form(""), payer: str = Form(""), gfe: str = Form(""),
             optin: str = Form("")):
    """Soft capture: mail the visitor a copy of their letter or comparison. Optional -
    everything is already on the page. Transactional, one email, rate-limited per IP."""
    to = capture.valid_email(email)
    if not to:
        return PlainTextResponse("Please enter a valid email address.", status_code=400)
    if not capture.configured():
        return PlainTextResponse("Email isn't enabled on this server yet - use the download button instead.")
    ip = request.client.host if request.client else "?"
    if not capture.allow(ip):
        return PlainTextResponse("Too many requests from your connection - try again in an hour.", status_code=429)
    kind = "compare" if kind == "compare" else "letter"
    st = _market(state)
    rows = _analyze(codes, st, payer or None, metro or None)
    if not any(r.get("resolved") for r in rows):
        return PlainTextResponse("Add a procedure or paste your bill first.", status_code=400)
    area = next((m["label"] for m in STATE["metros"]["list"] if m["key"] == metro), f"All {MARKETS[st]['name']}")
    link = f"{BASE_URL}/?state={st}" + (f"&metro={metro}" if metro else "")
    if kind == "letter":
        subject = "Your negotiation letter"
        body = (build_letter(rows, st, _money_in(gfe)) + f"\n\n-\nBuilt at {link}\n"
                "Estimates only - not a guarantee, not legal/medical/financial advice. "
                "This is the one email you asked for; we don't sell or share your address.")
    else:
        subject = f"Your hospital price comparison - {area}"
        body = capture.compare_summary(rows, MARKETS[st]["name"], area, link)
    ok = capture.send(to, subject, body)
    capture.record(to, kind, st, metro, codes[:2000], payer, optin == "on", ip, ok)
    return PlainTextResponse(f"Sent to {to}." if ok else "Couldn't send right now - use the download button instead.",
                             status_code=200 if ok else 502)


@app.post("/letter", response_class=PlainTextResponse)
def letter(codes: str = Form(""), state: str = Form(""), gfe: str = Form("")):
    """Download a negotiation/appeal letter built from the pasted bill."""
    text = build_letter(_analyze(codes, state), _market(state), _money_in(gfe))
    return PlainTextResponse(
        text, headers={"Content-Disposition": "attachment; filename=negotiation-letter.txt"})
