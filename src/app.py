"""Local web app: paste codes -> per-code pricing + negotiation talking points.

    uvicorn app:app --reload --app-dir src      (from repo root)
    # then open http://127.0.0.1:8000

No auth, no accounts (MVP). Medicare reference is loaded into an in-memory DuckDB
at startup from config/medicare.yaml; hospital rates are read from the Parquet
dataset produced by the ingest pipeline (run src/pipeline.py first to populate it).
"""
from __future__ import annotations

import json
import threading
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
from pricing import (CITY_COORDS, analyze, load_templates, load_services, load_hospital_names,
                     load_quality, load_hip_knee, load_freestanding, load_metros, load_geo,
                     load_contact,
                     payer_matrix, get_medicare_all, get_medicare, lookup)
from analysis import detect_issues, build_letter, savings_summary
from assistant import ask, build_context
from eob import extract_eob
from normalize import to_float
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

# When the hospital price dataset was last (re)built — surfaced for provenance/trust.
# Derived from the dataset itself (see _vintage) so it can't go stale the way the old
# hand-set "September 2026" string did. The DATA_REFRESHED env var pins it (tests, previews).
DATA_REFRESHED_PIN = os.environ.get("DATA_REFRESHED", "").strip()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# One codebase, two sites: APP_CONFIG=config (healthcare.traqqit.com, consumer) or
# config-ortho (ortho.traqqit.com, MSK price intelligence). Each has its own data volume.
CFG = os.path.join(ROOT, os.environ.get("APP_CONFIG", "config"))
BASE_URL = os.environ.get("BASE_URL", "https://healthcare.traqqit.com").rstrip("/")
SCHEMA = os.path.join(ROOT, "src", "schema.sql")
RATES_DIR = os.path.join(ROOT, "data", "hospital_rates")
PAYER_DIR = os.path.join(ROOT, "data", "payer_rates")
SITE_HOST = urllib.parse.urlsplit(BASE_URL).netloc or "healthcare.traqqit.com"


def _vintage():
    """'November 15, 2026': when the live dataset was last swapped in. The weekly rebuild
    builds each state partition in a staging dir and moves it into place, so the newest
    partition directory's mtime is the last successful swap. A few stat() calls, no cache."""
    if DATA_REFRESHED_PIN:
        return DATA_REFRESHED_PIN
    try:
        parts = [e for e in os.scandir(RATES_DIR) if e.is_dir() and e.name.startswith("state=")]
        ts = max(e.stat().st_mtime for e in parts)
    except (OSError, ValueError):
        return "date unavailable"
    import datetime
    d = datetime.datetime.fromtimestamp(ts)
    return f"{d:%B} {d.day}, {d.year}"

def _load_brand():
    """Site identity from <CFG>/brand.yaml; the consumer config has none -> defaults."""
    p = os.path.join(CFG, "brand.yaml")
    # Consumer site: ONE product name in titles and chrome (methodology used to say "Bill Coach"
    # while every other page said "Medical Price & Bill Coach"). SITE_NAME overrides it, since
    # the site may move to a new domain/name; BASE_URL already carries the hostname.
    name = os.environ.get("SITE_NAME", "").strip() or "Medical Price & Bill Coach"
    b = {"profile": "consumer", "name": name, "short": name, "tagline": "", "audience": ""}
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
    # Database-wide, so every per-thread cursor inherits them. The file cache kept the raw
    # parquet bytes resident (hundreds of MB); the rate tables below make it pointless anyway.
    con.execute("SET enable_external_file_cache=false")
    con.execute("SET memory_limit='512MB'")
    with open(os.path.join(CFG, "medicare.yaml"), encoding="utf-8") as f:
        med_cfg = yaml.safe_load(f)
    build_reference(con, ROOT, med_cfg)
    with open(os.path.join(CFG, "pricing.yaml"), encoding="utf-8") as f:
        multiplier = yaml.safe_load(f).get("target_multiplier", 2.0)
    services = load_services(os.path.join(CFG, "allowlist.yaml"))
    return {
        "con_base": con,
        "templates": load_templates(os.path.join(CFG, "templates.yaml")),
        "services": services,
        "descriptions": {(s["code"], s["type"]): s["desc"] for s in services},
        "hospital_names": load_hospital_names(os.path.join(CFG, "hospitals.yaml")),
        "hospital_stars": load_quality(os.path.join(CFG, "quality.yaml")),
        "hip_knee": load_hip_knee(os.path.join(CFG, "quality.yaml")),
        "metros": load_metros(os.path.join(CFG, "metros.yaml")),
        "geo": load_geo(os.path.join(CFG, "geo.yaml")),
        "contact": load_contact(os.path.join(CFG, "contact.yaml")),
        "freestanding": load_freestanding(os.path.join(CFG, "freestanding.yaml")),
        "multiplier": multiplier,
    }


STATE = _load()
_TLS = threading.local()


# ---------- Rate tables: the parquet datasets, loaded once into con_base ----------
# The ingest writes one tiny parquet file per (hospital, code), ~50k files. Globbing them on every
# query held 1.5-2 GB (DuckDB file cache + per-scan readers) and cost ~0.3 s a page. The rows fit
# in a few MB, so load them into tables and swap in a fresh copy when the files change.
import time
from pricing import set_rate_tables, _parquet_on_disk
from ingest import rates_source as _parquet_source

_TBL_LOCK = threading.Lock()
_TBL = {"stamp": None, "gen": 0, "prev": [], "cur": [], "checked": 0.0}
_TBL_CHECK_S = 60


def _data_stamp():
    """(dir count, newest mtime) over each dataset's top, state= and code= dirs: changes on the
    pipeline's partition swap and on an --ids append into an existing code dir."""
    out = []
    for d in (RATES_DIR, PAYER_DIR):
        if not os.path.isdir(d):
            out.append(None)
            continue
        ms = [os.stat(d).st_mtime_ns]
        for s in os.scandir(d):
            if s.is_dir():
                ms.append(s.stat().st_mtime_ns)
                ms.extend(c.stat().st_mtime_ns for c in os.scandir(s.path) if c.is_dir())
        out.append((len(ms), max(ms)))
    return tuple(out)


def _build_tables():
    """Build the next generation of tables, then repoint the resolver in one assignment, so a
    reader never sees a half-built table. The previous generation is kept for in-flight readers
    and dropped on the following swap. Call with _TBL_LOCK held."""
    stamp = _data_stamp()      # taken first: a change during the build triggers another one
    gen = _TBL["gen"] + 1
    con = STATE["con_base"].cursor()
    new, made = {}, []
    try:
        for name, d in (("hospital_rates", RATES_DIR), ("payer_rates", PAYER_DIR)):
            if not _parquet_on_disk(d):
                continue
            tbl = f"{name}_g{gen}"
            con.execute(f"CREATE TABLE {tbl} AS SELECT * FROM {_parquet_source(d)} ORDER BY state, code")
            made.append(tbl)
            n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            new[os.path.normpath(d)] = (tbl, n > 0)
    except Exception:
        for tbl in made:
            con.execute(f"DROP TABLE IF EXISTS {tbl}")
        raise
    set_rate_tables(new)
    for tbl in _TBL["prev"]:
        con.execute(f"DROP TABLE IF EXISTS {tbl}")
    _TBL.update(stamp=stamp, gen=gen, prev=_TBL["cur"], cur=made)
    print(f"rate tables: generation {gen} loaded ({', '.join(made) or 'no data'})", flush=True)


def _refresh_tables():
    if not _TBL_LOCK.acquire(blocking=False):
        return
    try:
        if _data_stamp() != _TBL["stamp"]:
            _build_tables()
    except Exception as ex:     # keep serving the current tables; retry on the next check
        print(f"rate tables: rebuild failed, keeping generation {_TBL['gen']}: {type(ex).__name__}: {ex}", flush=True)
    finally:
        _TBL_LOCK.release()


def _maybe_refresh_tables():
    """At most once a minute, look for new data off the request path."""
    now = time.monotonic()
    if now - _TBL["checked"] >= _TBL_CHECK_S:
        _TBL["checked"] = now
        threading.Thread(target=_refresh_tables, daemon=True).start()


with _TBL_LOCK:
    try:
        _build_tables()
    except Exception as _ex:    # no tables -> the resolver falls back to the parquet glob
        print(f"rate tables: initial load failed, using parquet glob: {type(_ex).__name__}: {_ex}", flush=True)
_TBL["checked"] = time.monotonic()


def _con():
    """Per-thread DuckDB handle. FastAPI runs sync routes in a threadpool, and one
    shared DuckDBPyConnection is not thread-safe: concurrent requests read each
    other's result sets. cursor() opens a new connection to the same in-memory DB."""
    _maybe_refresh_tables()
    c = getattr(_TLS, "con", None)
    if c is None:
        c = _TLS.con = STATE["con_base"].cursor()
    return c


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
    """A user-entered dollar amount ('$1,200' / '1200') to float; blank or zero -> None."""
    return to_float(s) or None


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
            "popular": STATE.get("popular", []), "data_refreshed": _vintage(),
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
    return analyze(_con(), RATES_DIR, codes, (state or DEFAULT_STATE).strip(),
                   STATE["templates"], STATE["descriptions"], STATE["multiplier"],
                   STATE["hospital_names"], STATE["hospital_stars"], PAYER_DIR,
                   payer or None, STATE["freestanding"], metro or None, metro_ids,
                   _latlon(lat, lon), STATE["geo"], STATE["hip_knee"])


@app.get("/", response_class=HTMLResponse)
def index(request: Request, state: str = "", metro: str = ""):
    """Home. `?state=CA` (or `?metro=denver`) pre-selects a market; unknown -> default."""
    ctx = dict(_ctx(None, "", state, "shop", metro=metro), nav="home")
    if CONSUMER:
        st = ctx["state"]
        ctx["guide"] = {"tiles": _tiles(st), "state_slug": MARKETS[st]["slug"],
                        "n_priced": next((x["n"] for x in _cov()["states"] if x["code"] == st), 0),
                        "n_cash": len([p for p in _cash_providers(st) if p["n_priced"]])}
    return templates.TemplateResponse(request, "index.html", ctx)


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
    phrase = _mid_sentence(desc)            # for use inside sentences; `desc` stays the name
    city, path = _metro_meta(metro, state)
    counts = _fac_counts(d)
    low, high = d.get("neg_low"), d.get("neg_high")
    price_bit = (f"from ${low:,.0f}" if low is not None else "prices")
    meta = (f"Compare {phrase} (CPT {code}) prices across {counts['phrase']} in {city} — {price_bit}. "
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
    faqs = _build_faqs(phrase, code, city, d, counts)
    crumbs = _procedure_crumbs(state, metro, code, city, path)
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "MedicalProcedure", "name": desc, "url": canonical,
         "code": {"@type": "MedicalCode", "code": code, "codingSystem": "CPT"}},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": c["name"],
             "item": f"{BASE_URL}{c['url']}" if c.get("url") else canonical}
            for i, c in enumerate(crumbs)]},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": f["q"],
             "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faqs]},
    ]}
    detail = _procedure_detail(code, state, metro, d) if CONSUMER else None
    return templates.TemplateResponse(request, "procedure.html", {
        "detail": detail,
        "r": r, "city": city, "metro": metro, "desc": desc, "phrase": phrase, "base": BASE_URL, "brand": BRAND,
        "n_hosp": MARKETS[state].get("n", 0), "nav": "compare", "own_ids": OWN_IDS,
        "canonical": canonical, "faqs": faqs, "ld": ld, "data_refreshed": _vintage(), "counts": counts,
        "crumbs": crumbs,
        "cash_rows": (cashpay.for_code(CASH, code, state, metro or None) if CONSUMER else []),
        "title": f"How much does {phrase} (CPT {code}) cost in {city}? | {BRAND['short']}",
        "meta_desc": meta, "siblings": siblings, "related": related,
    })


def _procedure_detail(code, state, metro, d):
    """Comparison context for a consumer procedure page: metro vs state typical with n,
    insurer medians with hospital counts, and the category it belongs to."""
    import hubs
    out = {"state_name": MARKETS[state]["name"], "state_typical": None, "state_n": 0, "payers": [],
           "cat": None, "cat_url": "", "related": []}
    if metro:
        srows = [r for r in _analyze(code, state, None, None) if r["resolved"]]
        if srows:
            out["state_typical"] = srows[0]["result"].get("neg_median")
            out["state_n"] = srows[0]["result"].get("n_hospitals") or 0
    try:
        pm = payer_matrix(_con(), RATES_DIR, PAYER_DIR, code, state, STATE["hospital_names"],
                          STATE["metros"]["ids"].get(metro) if metro else None, PAYER_LIST)
        out["payers"] = sorted(({"payer": p, "median": pm["stats"][p]["median"], "n": pm["stats"][p]["n"]}
                                for p in pm["payers"] if p in pm["stats"]), key=lambda x: x["median"])
    except Exception:             # payer data is optional context; never fail the page on it
        out["payers"] = []
    cat = hubs.category_of(code)
    if cat:
        c = hubs.CAT_BY_SLUG[cat]
        base = f"/{MARKETS[state]['slug']}" + (f"/{metro}" if metro else "")
        out.update(cat=c[1], cat_url=f"{base}/{cat}")
        proc_base = f"/{metro}/procedure/" if metro else f"{_state_path(state)}/procedure/"
        out["related"] = [{"desc": _by_code[k]["desc"], "code": k, "url": f"{proc_base}{k}"}
                          for k in c[4] if k != code and k in _by_code]
    return out


def _procedure_crumbs(state, metro, code, city, path):
    """Home › Arizona › Phoenix Metro › MRI › CPT 73721 (consumer). Ortho keeps Home › area › code.
    Hub links resolve on the consumer profile only."""
    import hubs
    if BRAND.get("profile") == "ortho":
        return [{"name": "Home", "url": "/"}, {"name": city, "url": path.rstrip("/").rsplit("/procedure", 1)[0] or "/"},
                {"name": code}]
    st_slug = MARKETS[state]["slug"]
    out = [{"name": "Home", "url": "/"}, {"name": MARKETS[state]["name"], "url": f"/{st_slug}"}]
    base = f"/{st_slug}"
    if metro:
        m = next((x for x in STATE["metros"]["list"] if x["key"] == metro), None)
        out.append({"name": m["label"] if m else city, "url": f"/{st_slug}/{metro}"})
        base = f"/{st_slug}/{metro}"
    cat = hubs.category_of(code)
    if cat:
        out.append({"name": hubs.CAT_BY_SLUG[cat][1], "url": f"{base}/{cat}"})
    out.append({"name": f"CPT {code}" if code[:1].isdigit() or code[:1] == "G" else code})
    return out


def _fac_counts(d):
    """One facility count for a procedure page, used by title, meta, hero, table and FAQ alike.
    The table lists hospitals AND independent imaging/surgery centers; the typical range is
    computed over hospital prices only (n_hospitals). Saying '27 facilities' above a 31-row
    table was the trust bug — so name both numbers."""
    facs = d.get("hospitals") or []
    n_h = sum(1 for h in facs if (h.get("type") or "hospital") == "hospital")
    n_ind = len(facs) - n_h
    phrase = (f"{len(facs)} facilities ({n_h} hospitals, {n_ind} independent "
              f"{'center' if n_ind == 1 else 'centers'})" if n_ind else
              f"{n_h} {'hospital' if n_h == 1 else 'hospitals'}")
    return {"total": len(facs), "hospitals": n_h, "independent": n_ind,
            "in_range": d.get("n_hospitals") or 0, "phrase": phrase}


_KEEP_CAPS = {"Achilles"}   # proper nouns that can open a procedure name


def _mid_sentence(desc):
    """'Total knee replacement' -> 'total knee replacement', for use inside a sentence.
    Leaves acronyms (MRI, CT, X-ray, C-reactive, SARS-CoV-2) and proper nouns alone."""
    first = desc.split(" ", 1)[0].rstrip(",")
    if first in _KEEP_CAPS or not first[1:2].islower():
        return desc
    return desc[:1].lower() + desc[1:]


def _build_faqs(desc, code, city, d, counts=None):
    """Visible + schema FAQ, grounded in this page's numbers."""
    def m(v):
        return f"${v:,.0f}" if v is not None else None
    low, med, high = d.get("neg_low"), d.get("neg_median"), d.get("neg_high")
    mc = d.get("medicare_rate")
    counts = counts or _fac_counts(d)
    faqs = []
    if med is not None:
        faqs.append({"q": f"How much does {desc} cost in {city}?",
                     "a": (f"The typical hospital price for {desc} (CPT {code}) in {city} is about "
                           f"{m(med)} (each hospital's published cash price where it has one, otherwise "
                           f"its negotiated insurer median). The middle half of the {counts['in_range']} hospital prices "
                           f"runs from {m(low)} to {m(high)}; the comparison table lists "
                           f"{counts['phrase']}. These are estimates from published prices, "
                           f"not a guarantee.")})
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
    is_cat = CONSUMER and proc_slug in hubs.CAT_BY_SLUG
    if not state or STATE["metros"]["state_of"].get(metro) != state or not (proc_slug in SLUG_TO_CODE or is_cat):
        return HTMLResponse("Unknown page.", status_code=404)
    if state != _market(state):
        return _coming_soon(state)
    if proc_slug not in SLUG_TO_CODE:                     # /arizona/phoenix/mri: a category hub
        return _category_hub(request, state, metro, proc_slug)
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
    m = payer_matrix(_con(), RATES_DIR, PAYER_DIR, svc["code"], state,
                     STATE["hospital_names"], metro_ids, PAYER_LIST)
    for r in m["rows"]:
        r["hip_knee"] = STATE["hip_knee"].get(r["id"])
    cols = ["cash", "median"] + m["payers"]
    med_rate, med_src = get_medicare(_con(), svc["code"], svc["type"])
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
        "data_refreshed": _vintage()})


@app.get("/rates.csv")
def rates_csv(code: str = "", metro: str = ""):
    """The same matrix as CSV — contracting teams live in spreadsheets."""
    import csv
    import io
    svc = _svc(code)
    state, metro, metro_ids = _area(metro)
    m = payer_matrix(_con(), RATES_DIR, PAYER_DIR, svc["code"], state,
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
    d = lookup(_con(), RATES_DIR, svc["code"], svc["type"], state, svc["desc"],
               STATE["multiplier"], STATE["hospital_names"], STATE["hospital_stars"], PAYER_DIR,
               None, STATE["freestanding"], metro or None, metro_ids, None, STATE["geo"], STATE["hip_knee"])
    hosp = [h for h in d["hospitals"] if h["type"] == "hospital" and h["price"] is not None]
    asc = [h for h in d["hospitals"] if h["type"] != "hospital" and h["price"] is not None]
    import statistics
    # Shift math uses facility-type sites only: MDsave "bundles" include surgeon/anesthesia fees,
    # so they are not comparable to a facility rate (they stay in the table, labelled).
    sites = [a["price"] for a in asc if a["type"] in ("asc", "imaging", "office")]
    asc_median = statistics.median(sites) if sites else None
    med = get_medicare_all(_con(), svc["code"], svc["type"])
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
        "brand": BRAND, "nav": "site", "data_refreshed": _vintage()})


@app.get("/position", response_class=HTMLResponse)
def position(request: Request, facility: str = "", metro: str = ""):
    """Our facility vs the market: every tracked code we publish a price for, our price beside
    the competitors' median, Medicare multiples, percentile, and payer-by-payer pairs."""
    from pricing import rates_source
    from pricing import _has_parquet
    own_list = [{"id": i, "name": STATE["hospital_names"].get(i, i)} for i in sorted(OWN_IDS)]
    if not own_list:
        return HTMLResponse("No own facilities configured (brand.yaml `own.ids`).", status_code=404)
    fac_id = facility if facility in OWN_IDS else own_list[0]["id"]
    state, metro, metro_ids = _area(metro, HOSP_STATE.get(fac_id, DEFAULT_STATE))
    codes = []
    if _has_parquet(RATES_DIR):
        codes = [c for (c,) in _con().execute(
            f"SELECT DISTINCT code FROM {rates_source(RATES_DIR)} WHERE hospital_id=? AND state=?",
            [fac_id, state]).fetchall()]
    rows, payers_seen = [], set()
    for svc in STATE["services"]:
        if svc["code"] not in codes:
            continue
        m = payer_matrix(_con(), RATES_DIR, PAYER_DIR, svc["code"], state,
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
        med_rate, _ = get_medicare(_con(), svc["code"], svc["type"])
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
        "data_refreshed": _vintage()})


# ---------- Payer scorecard: every payer x every code, one parquet scan, cached per refresh ----------

@lru_cache(maxsize=16)
def _payer_table(state, metro, stamp):
    """{ (code, payer): {'median': m, 'n': n}, ('own', code, payer): rate } over the market."""
    from pricing import rates_source
    from pricing import _has_parquet
    import statistics
    out = {}
    if not _has_parquet(PAYER_DIR):
        return out
    metro_ids = STATE["metros"]["ids"].get(metro) if metro else None
    acc = {}
    for hid, code, payer, rate in _con().execute(
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
        med, _ = get_medicare(_con(), code, svc["type"]) if svc else (None, None)
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
    stamp = (os.path.getmtime(PAYER_DIR) if os.path.isdir(PAYER_DIR) else 0, _TBL["gen"])
    tbl = _payer_table(state, metro, stamp)
    rows = []
    for svc in STATE["services"]:
        med, _ = get_medicare(_con(), svc["code"], svc["type"])
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
        "data_refreshed": _vintage()})


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
    d = lookup(_con(), RATES_DIR, fcode, fs["type"], state, fs["desc"], STATE["multiplier"],
               STATE["hospital_names"], STATE["hospital_stars"], PAYER_DIR, None, STATE["freestanding"],
               metro or None, metro_ids, None, STATE["geo"], STATE["hip_knee"])
    med_all = get_medicare_all(_con(), fcode, fs["type"])
    hosp_typ = d.get("neg_median")
    sites = [h["price"] for h in d["hospitals"] if h["type"] in ("asc", "imaging", "office") and h["price"] is not None]
    asc_pub = statistics.median(sites) if sites else None
    asc_est = (hosp_typ * med_all["ASC"] / med_all["OPPS"]
               if (asc_pub is None and hosp_typ and med_all.get("ASC") and med_all.get("OPPS")) else None)
    own_fac = None
    if OWN_IDS:
        m = payer_matrix(_con(), RATES_DIR, PAYER_DIR, fcode, state, STATE["hospital_names"], None, PAYER_LIST)
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
        rates = get_medicare_all(_con(), code, svc_of(code)["type"])
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
        dm = payer_matrix(_con(), RATES_DIR, PAYER_DIR, ep["drg"], state, STATE["hospital_names"], metro_ids, PAYER_LIST)
        others = [(r["median"] if r["median"] is not None else r["cash"]) for r in dm["rows"] if r["id"] not in OWN_IDS]
        others = [x for x in others if x is not None]
        own_drg = next(((r["median"] if r["median"] is not None else r["cash"]) for r in dm["rows"] if r["id"] in OWN_IDS), None)
        ipps, _ = get_medicare(_con(), ep["drg"], "DRG")
        drg_row = {"market": statistics.median(others) if others else None, "n": len(others), "ipps": ipps, "own": own_drg}
    my = _money_in(mine)
    return templates.TemplateResponse(request, "bundles.html", {
        "brand": BRAND, "nav": "bundles", **_area_ctx(state, metro),
        "ep": ep, "episodes": eps, "cfg": BUNDLES, "cols": cols, "comps": comps, "drg_row": drg_row,
        "mine": my, "mine_raw": mine,
        "own_label": own_label, "own_col": bool(own_fac), "data_refreshed": _vintage()})


_COV_CACHE = {}


def _independents(state=None):
    """Independent (non-hospital) facilities with at least one published price: the inline
    freestanding imaging/ASC list (in the compare tables) plus the cash-pay directory."""
    names = {(f["state"], f["name"]) for rows in STATE["freestanding"].values() for f in rows
             if f.get("price") is not None and (state is None or f["state"] == state)}
    cash = CASH["providers"] if "CASH" in globals() else []
    names |= {(p["state"], p["name"]) for p in cash if p["n_priced"] and (state is None or p["state"] == state)}
    return len(names)


def _coverage(stamp):
    """Live coverage numbers for /methodology and /data-coverage, cached per data refresh stamp.
    Only a non-empty result is cached: a read that lands mid-refresh or during warm-up
    used to pin '0 facilities' until the next data refresh changed the stamp."""
    if stamp in _COV_CACHE:
        return _COV_CACHE[stamp]
    from pricing import rates_source
    from pricing import _has_parquet
    cov = {"hospitals": 0, "codes": len(STATE["services"]), "rows": 0, "payers": len(PAYER_LIST),
           "states": [], "independent": _independents(), "vintage": _vintage()}
    payers = {}
    if _has_parquet(PAYER_DIR):
        payers = dict(_con().execute(
            f"SELECT state, count(DISTINCT payer) FROM {rates_source(PAYER_DIR)} GROUP BY 1").fetchall())
    if _has_parquet(RATES_DIR):
        for st, n, codes, rows in _con().execute(
                f"SELECT state, count(DISTINCT hospital_id), count(DISTINCT code), count(*) "
                f"FROM {rates_source(RATES_DIR)} GROUP BY 1 ORDER BY 1").fetchall():
            if st in MARKETS:
                cov["states"].append({"code": st, "name": MARKETS[st]["name"], "slug": MARKETS[st]["slug"],
                                      "n": n, "configured": MARKETS[st].get("n", 0), "codes": codes,
                                      "rows": rows, "payers": payers.get(st, 0),
                                      "independent": _independents(st)})
            cov["hospitals"] += n
            cov["rows"] += rows
    cov["facilities"] = cov["hospitals"] + cov["independent"]
    if cov["hospitals"]:
        _COV_CACHE.clear()
        _COV_CACHE[stamp] = cov
    return cov


def _cov():
    # Keyed on the rate-table generation too, so a table rebuild refreshes the counts.
    return _coverage((os.path.getmtime(RATES_DIR) if os.path.isdir(RATES_DIR) else 0, _TBL["gen"]))


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


def _pct_in(s):
    """A user-entered coinsurance percentage, clamped to 0-100. '20' -> 20.0, '0.2' -> 0.2,
    '200' -> 100.0. Unclamped this number is multiplied by a price and read aloud to a patient."""
    v = to_float(s)
    return None if v is None else max(0.0, min(100.0, v))


def _patient_cost(price, ded_left, coins):
    """What the patient pays out of pocket, as a plain estimate: the remaining deductible
    first, then coinsurance on the rest. Both inputs optional; returns None without them.
    Ignores out-of-pocket maximums and any facility fee the plan carves out separately."""
    if price is None or (ded_left is None and coins is None):
        return None
    ded = min(price, ded_left or 0)
    return round(ded + (price - ded) * ((coins if coins is not None else 0) / 100.0))


def _radii(rad):
    """The radius choices, always including the one in force -- otherwise a shared
    ?radius=30 link renders a select showing '5 mi' while the results say 30."""
    return sorted({5, 10, 25, 50, 100, rad})


def _market_near(center):
    """The market the searched point actually sits in, by nearest known facility.
    The Area select cannot be trusted for this: a Richmond ZIP priced against Arizona
    returns a confident 'no facility within 25 miles', which is the worst possible answer.
    O(301) over coordinates already in memory -- no geocoder round trip."""
    best, best_d = "", 1e18
    for hid, coords in (STATE["geo"] or {}).items():
        if not coords:
            continue
        d = (coords[0] - center[0]) ** 2 + (coords[1] - center[1]) ** 2
        if d < best_d:
            best, best_d = HOSP_STATE.get(hid, ""), d
    return best if best in MARKETS else ""


def _near(svc, state, q, radius, payer=None, sort="price", pinned=True):
    """Facilities within `radius` miles of an address/ZIP, priced for one insurer if given.
    Shared by /map and /refer, so both always show the same number for the same patient.
    `pinned` = the user explicitly chose an area, so the geocoder may be biased to it.
    -> (center, rows, err, radius, medicare_rate, state, moved) where `state` may differ from
    the one passed in (the searched point wins) and `moved` names it when it does."""
    try:
        rad = min(max(int(radius), 5), 200)
    except (TypeError, ValueError):
        rad = 25
    center = err = None
    moved = ""
    if (q or "").strip():
        try:
            # Bias to the chosen market only when the user actually chose one. Biasing by
            # default turned '1200 E Broad St, Richmond' into a point in Arizona.
            center = _geocode(q.strip(), MARKETS[state]["name"] if pinned else "")
        except Exception:
            err = "Couldn't find that address or ZIP. Try a ZIP code or 'street, city'."
    if center:
        here = _market_near(center)
        if here and here != state:
            state, moved = here, MARKETS[here]["name"]
    rows, med_rate = [], None
    if center:
        d = _analyze(svc["code"], state, payer or None, None, center[0], center[1])
        hosp = d[0]["result"]["hospitals"] if d and d[0]["resolved"] else []
        rows = [h for h in hosp if h.get("distance") is not None and h["distance"] <= rad]
        for h in rows:
            # freestanding/ASC rows carry id None; pricing.py already places them by city
            # centroid for the distance column, so use the same fallback for the map
            h["lat"], h["lon"] = (STATE["geo"].get(h["id"]) if h["id"]
                                  else CITY_COORDS.get(h.get("city"))) or (None, None)
            h["hip_knee"] = STATE["hip_knee"].get(h["id"]) if h["id"] else None
            h["contact"] = STATE["contact"].get(h["id"]) if h["id"] else None
        # the 'lowest' badge came from the state-wide breakdown and was then radius-filtered
        # away, so a radius search often had no highlighted row at all. Re-flag inside the
        # radius, and never crown a row price_stats already flagged as a likely data error.
        for h in rows:
            h["cheapest"] = False
        sane = [h for h in rows if h["price"] is not None and h.get("outlier") != "low"]
        if sane:
            min(sane, key=lambda h: (h["price"], h["distance"], h["name"]))["cheapest"] = True
        # a facility with no published rate for THIS payer sorts last, never first
        big = 1e12
        # Every sort ends in (distance, name) so it is a TOTAL order. Without it, the many
        # facilities that share one payer's median price tied, the underlying row order was not
        # stable across requests, and the same patient lookup ranked a different facility #1 on
        # each refresh -- so a link shared between two people at the desk showed two answers.
        def tie(h):
            return (h["distance"], h["name"])
        if sort == "dist":
            rows.sort(key=lambda h: tie(h))
        elif sort == "quality":
            # lowest complication rate first; unscored facilities after the scored ones
            rows.sort(key=lambda h: (h["hip_knee"]["rate"] if h.get("hip_knee") else big,) + tie(h))
        else:
            rows.sort(key=lambda h: (h.get("outlier") == "low",
                                     h["price"] if h["price"] is not None else big) + tie(h))
        med_rate, _ = get_medicare(_con(), svc["code"], svc["type"])
    return center, rows, err, rad, med_rate, state, moved


@app.get("/map", response_class=HTMLResponse)
def near_map(request: Request, code: str = "", q: str = "", radius: str = "25", sort: str = "price",
             metro: str = "", payer: str = "", ded: str = "", coins: str = ""):
    """Facilities within a radius of an address or ZIP for one procedure, on a map."""
    svc = _svc(code)
    state, metro, _ = _area(metro)
    sel = payer if payer in PAYER_LIST else ""
    center, rows, err, rad, med_rate, state, moved = _near(svc, state, q, radius, sel, sort,
                                                           pinned=bool(metro))
    if moved:
        metro = ""
    ded_v, coins_v = _money_in(ded), _pct_in(coins)
    for h in rows:
        h["patient"] = _patient_cost(h["price"], ded_v, coins_v)
    return templates.TemplateResponse(request, "map.html", {
        "svc": svc, "q": q, "radius": rad, "sort": sort, "center": center, "err": err, "rows": rows,
        "medicare": med_rate, "radii": _radii(rad), "groups": _groups(), "cat_labels": CAT_LABELS,
        "payers": PAYER_LIST, "sel": sel, "ded": ded, "coins": coins, "moved": moved,
        "brand": BRAND, "nav": "map", **_area_ctx(state, metro), "own_ids": OWN_IDS,
        "data_refreshed": _vintage()})


@app.get("/refer", response_class=HTMLResponse)
def refer(request: Request, code: str = "", q: str = "", radius: str = "25", metro: str = "",
          payer: str = "", ded: str = "", coins: str = "", sort: str = "price", n: str = "5"):
    """Referral finder: procedure + patient ZIP + insurance -> a short ranked list a care
    manager can act on in under a minute. Same data as /map, shaped for one decision."""
    svc = _svc(code)
    state, metro, _ = _area(metro)
    sel = payer if payer in PAYER_LIST else ""
    center, rows, err, rad, med_rate, state, moved = _near(svc, state, q, radius, sel, sort,
                                                           pinned=bool(metro))
    if moved:
        metro = ""
    ded_v, coins_v = _money_in(ded), _pct_in(coins)
    for h in rows:
        h["patient"] = _patient_cost(h["price"], ded_v, coins_v)
    try:
        top = max(3, min(int(n), 10))
    except ValueError:
        top = 5
    shown, more = rows[:top], max(0, len(rows) - top)
    return templates.TemplateResponse(request, "refer.html", {
        "svc": svc, "q": q, "radius": rad, "sort": sort, "center": center, "err": err,
        "rows": shown, "more": more, "n": top, "medicare": med_rate, "moved": moved,
        "radii": _radii(rad), "groups": _groups(), "cat_labels": CAT_LABELS,
        "payers": PAYER_LIST, "sel": sel, "ded": ded, "coins": coins,
        "brand": BRAND, "nav": "refer", **_area_ctx(state, metro), "own_ids": OWN_IDS,
        "data_refreshed": _vintage()})


@app.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request):
    """Where every number comes from, with the data-flow diagram. Same page on both profiles."""
    with open(os.path.join(CFG, "medicare.yaml"), encoding="utf-8") as f:
        med = yaml.safe_load(f) or {}
    med_sources = {s for (s,) in _con().execute("SELECT DISTINCT source FROM medicare_rates").fetchall()}
    return templates.TemplateResponse(request, "methodology.html", {
        "brand": BRAND, "base": BASE_URL, "nav": "method", "cov": _cov(),
        "state_name": MARKETS[DEFAULT_STATE]["name"], "n_hosp": MARKETS[DEFAULT_STATE].get("n", 0),
        "med_year": med.get("year", 2025), "pfs_cf": med.get("pfs_conversion_factor", ""),
        "med_sources": med_sources, "has_hk": bool(STATE["hip_knee"]), "data_refreshed": _vintage(),
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
    if CONSUMER:
        urls += [f"{BASE_URL}{p}" for p in ("/procedures", "/cash", "/data-coverage", "/trust", "/corrections",
                                            "/faq", "/about", "/privacy")]
        for st in live:
            sl = MARKETS[st]["slug"]
            urls += [f"{BASE_URL}/{sl}", f"{BASE_URL}/{sl}/hospitals"]
            urls += [f"{BASE_URL}/{sl}/{c[0]}" for c in hubs.CATEGORIES]
            for m in STATE["metros"]["list"]:
                if m["state"] == st:
                    urls.append(f"{BASE_URL}/{sl}/{m['key']}")
                    if m["promote"]:
                        urls += [f"{BASE_URL}/{sl}/{m['key']}/{c[0]}" for c in hubs.CATEGORIES]
            urls += [f"{BASE_URL}{_hosp_url(h['id'])}" for h in _state_hospitals(st) if h["codes"] and h["url"]]
        urls += [f"{BASE_URL}{_cash_url(p)}" for p in CASH["providers"]]
        urls += [f"{BASE_URL}{_freestanding_url(e['name'])}" for e in FREESTANDING_SLUG.values() if e["state"] in live]
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


# ---------- Consumer price guide: site chrome pages, hubs, facility index ----------
# healthcare.traqqit.com only (ortho keeps its own nav; these routes 404 there, except
# /privacy). Hostname and site name come from BASE_URL / SITE_NAME, so a domain move is
# an env change, not a code change.
import hubs  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

CONSUMER = BRAND.get("profile") != "ortho"
CORRECTIONS_SLA = os.environ.get("CORRECTIONS_SLA", "7 days")
HOSP_URL, HOSP_CCN = {}, {}
with open(os.path.join(CFG, "hospitals.yaml"), encoding="utf-8") as _f:
    for _h in yaml.safe_load(_f)["hospitals"]:
        HOSP_URL[_h["id"]] = _h.get("url") or ""
        HOSP_CCN[_h["id"]] = str(_h.get("ccn") or "")


def _consumer_only():
    if not CONSUMER:
        raise HTTPException(status_code=404)


def _crumb_ld(crumbs):
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": c["name"],
         **({"item": f"{BASE_URL}{c['url']}"} if c.get("url") else {})} for i, c in enumerate(crumbs)]}


def _page(request, tpl, title, meta_desc, path, nav="", crumbs=None, ld=None, status=200, **ctx):
    """Render a guide page through _layout.html with the shared chrome context."""
    graph = [g for g in ([_crumb_ld(crumbs)] if crumbs else []) + ([ld] if ld else [])]
    data = {"brand": BRAND, "base": BASE_URL, "site_host": SITE_HOST, "data_refreshed": _vintage(),
            "title": title, "meta_desc": meta_desc, "canonical": f"{BASE_URL}{path}", "nav": nav,
            "crumbs": crumbs or [], "cov": _cov(),
            "ld": {"@context": "https://schema.org", "@graph": graph} if graph else None}
    data.update(ctx)
    return templates.TemplateResponse(request, tpl, data, status_code=status)


def _home_crumb():
    return {"name": "Home", "url": "/"}


def _state_crumb(st):
    return {"name": MARKETS[st]["name"], "url": f"/{MARKETS[st]['slug']}"}


_HOSP_STATS = {}


def _hosp_stats():
    """hospital_id -> number of tracked codes it publishes a usable price for (per data stamp)."""
    stamp = os.path.getmtime(RATES_DIR) if os.path.isdir(RATES_DIR) else 0
    if stamp in _HOSP_STATS:
        return _HOSP_STATS[stamp]
    from pricing import rates_source
    from pricing import _has_parquet
    out = {}
    if _has_parquet(RATES_DIR):
        out = dict(_con().execute(
            f"SELECT hospital_id, count(DISTINCT code) FROM {rates_source(RATES_DIR)} "
            f"WHERE coalesce(cash_price, negotiated_median) > 0 GROUP BY 1").fetchall())
    if out:
        _HOSP_STATS.clear()
        _HOSP_STATS[stamp] = out
    return out


def _hosp_slug(hid):
    return hubs.slugify(hid)


def _hosp_url(hid):
    st = HOSP_STATE.get(hid)
    mk = STATE["metros"]["of"].get(hid)
    if not st or st not in MARKETS or not mk:
        return ""
    return f"/{MARKETS[st]['slug']}/{mk}/hospitals/{_hosp_slug(hid)}"


def _state_hospitals(st):
    """Every configured hospital in a state, priced ones first, with metro + profile link."""
    stats = _hosp_stats()
    rows = []
    for hid, hst in HOSP_STATE.items():
        if hst != st:
            continue
        mk = STATE["metros"]["of"].get(hid, "")
        rows.append({"id": hid, "name": STATE["hospital_names"].get(hid, hid), "metro": mk,
                     "metro_label": next((m["label"] for m in STATE["metros"]["list"] if m["key"] == mk), ""),
                     "codes": stats.get(hid, 0), "stars": STATE["hospital_stars"].get(hid),
                     "url": _hosp_url(hid)})
    rows.sort(key=lambda r: (r["codes"] == 0, r["metro_label"], r["name"]))
    return rows


def _freestanding_list(st=None, metro=None):
    """Independent facilities from config/freestanding.yaml: name -> {type, city, metro, n codes}."""
    out = {}
    for code, facs in STATE["freestanding"].items():
        for f in facs:
            if (st and f["state"] != st) or (metro and f.get("metro") != metro):
                continue
            e = out.setdefault(f["name"], {"name": f["name"], "type": f["type"], "city": f["city"],
                                           "metro": f.get("metro"), "state": f["state"], "codes": 0})
            e["codes"] += 1
    return sorted(out.values(), key=lambda e: e["name"])


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return _page(request, "privacy.html", f"Privacy | {BRAND['short']}",
                 "What this site collects (very little), what it doesn't, and how to have your data removed.",
                 "/privacy", crumbs=[_home_crumb(), {"name": "Privacy"}])


@app.get("/trust", response_class=HTMLResponse)
def trust(request: Request):
    _consumer_only()
    return _page(request, "trust.html", f"Trust & limits | {BRAND['short']}",
                 "No paid placement, no made-up prices: what cash, prepaid, negotiated, typical, and ask "
                 "prices mean on this site, and what they leave out.",
                 "/trust", crumbs=[_home_crumb(), {"name": "Trust & limits"}])


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    _consumer_only()
    return _page(request, "about.html", f"About | {BRAND['short']}",
                 "An independent price guide built from hospital price files and providers' own cash price lists.",
                 "/about", crumbs=[_home_crumb(), {"name": "About"}])


def _faqs():
    v = _vintage()
    return [
        {"q": "What's the difference between a cash price and an insurance price?",
         "a": "A cash (self-pay) price is what a facility says it charges people paying for themselves. "
              "An insurance or negotiated price is the rate a hospital agreed with an insurer; with insurance "
              "you pay part of it through your deductible and coinsurance. Cash can be lower than your "
              "insurance price, especially for imaging and labs, so compare both. Each price on this site "
              "is labeled <b>cash</b>, <b>prepaid bundle</b>, or <b>negotiated</b>."},
        {"q": "Does the price include the doctor?",
         "a": "Usually not. Hospital price files mostly cover the facility fee. Radiologist reads, surgeon, "
              "and anesthesia fees are often billed separately. Some independent cash prices and prepaid "
              "bundles do include the read; the listing says so when the provider does. Ask for a written "
              "Good Faith Estimate before you book."},
        {"q": "How current is the data?",
         "a": f"Hospital prices are rebuilt from every hospital's published file each week; the last "
              f"rebuild was {v}. Independent cash prices are checked by hand, and each one shows the date we "
              f"checked it and, when the source shows one, the date of the source document."},
        {"q": "Why isn't my hospital or imaging center listed?",
         "a": "Hospitals must publish price files, but some files are missing or unusable in a given week; "
              "<a href='/data-coverage'>data coverage</a> lists them. Independent centers aren't required to "
              "publish prices, so we can only list the ones that do. <a href='/corrections'>Send us</a> a "
              "public price link and we'll review it."},
        {"q": "Can a facility pay to be listed or ranked higher?",
         "a": "No. There is no paid placement of any kind. Lists are ordered by price, then by how complete "
              "and current the price data is."},
        {"q": "How do I use the bill coach?",
         "a": "Paste your itemized bill (or the codes and amounts) on the <a href='/'>home page</a> under "
              "\"Analyze my bill\". You'll see what each line should cost, the Medicare benchmark, a "
              "suggested ask, and a ready-to-send appeal letter. Nothing is required to browse prices."},
        {"q": "Is this a quote?",
         "a": "No. These are estimates from public data to help you compare and negotiate, not a quote, a "
              "contract, or medical, legal, or financial advice. Confirm the price with the provider."},
    ]


@app.get("/faq", response_class=HTMLResponse)
def faq(request: Request):
    _consumer_only()
    faqs = _faqs()
    import re as _re
    ld = {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f["q"],
         "acceptedAnswer": {"@type": "Answer", "text": _re.sub(r"<[^>]+>", "", f["a"])}} for f in faqs]}
    return _page(request, "faq.html", f"FAQ | {BRAND['short']}",
                 "Cash vs insurance prices, whether the doctor is included, how fresh the data is, and how "
                 "to use the bill coach.", "/faq", crumbs=[_home_crumb(), {"name": "FAQ"}], ld=ld, faqs=faqs)


@app.get("/data-coverage", response_class=HTMLResponse)
def data_coverage(request: Request):
    _consumer_only()
    cov = _cov()
    stats = _hosp_stats()
    gaps = {s["code"]: [h for h in _state_hospitals(s["code"]) if not stats.get(h["id"])] for s in cov["states"]}
    return _page(request, "data_coverage.html", f"Data coverage | {BRAND['short']}",
                 "Hospitals and independent facilities with prices, codes tracked, price rows, insurers, "
                 "and the last rebuild date, state by state.", "/data-coverage",
                 crumbs=[_home_crumb(), {"name": "Data coverage"}], gaps=gaps)


@app.get("/corrections", response_class=HTMLResponse)
def corrections(request: Request, sent: str = "", facility: str = "", procedure: str = "", page: str = ""):
    """The page + form. ?facility=&procedure=&page= prefill it from a listing's 'correct this' link."""
    _consumer_only()
    return _corrections_page(request, f={"facility": facility[:200], "procedure": procedure[:200],
                                         "page": page[:200], "kind": "wrong_price" if facility else ""}, sent=sent)


def _services_by_category():
    """[(category tuple, [service, ...])] in hub order, then any tracked code not in a hub."""
    out, seen = [], set()
    for c in hubs.CATEGORIES:
        items = [_by_code[k] for k in c[4] if k in _by_code]
        seen.update(s["code"] for s in items)
        if items:
            out.append((c, items))
    rest = [s for s in STATE["services"] if s["code"] not in seen]
    if rest:
        out.append((("other", "Other procedures", "", "", []), rest))
    return out


@app.get("/procedures", response_class=HTMLResponse)
def procedures(request: Request):
    _consumer_only()
    st = DEFAULT_STATE
    return _page(request, "procedures.html", f"All procedures with {MARKETS[st]['name']} prices | {BRAND['short']}",
                 f"Every procedure we track with hospital and cash prices in {MARKETS[st]['name']}, by category.",
                 "/procedures", nav="procedures", crumbs=[_home_crumb(), {"name": "Procedures"}],
                 groups=_services_by_category(), state=st, state_slug=MARKETS[st]["slug"])


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", area: str = ""):
    _consumer_only()
    qs = (q or "").strip()
    code = qs.upper()
    area = area if area in STATE["metros"]["ids"] else ""
    if code in _by_code:
        return RedirectResponse(f"/{area}/procedure/{code}" if area else f"/procedure/{code}", status_code=302)
    words = [w for w in qs.lower().replace(",", " ").split() if w]
    hits = []
    if words:
        for s in STATE["services"]:
            cat = hubs.category_of(s["code"])
            hay = (f"{s['desc']} {s['code']} {hubs.CAT_BY_SLUG[cat][1] if cat else ''} "
                   f"{hubs.ALIASES.get(s['code'], '')}").lower()
            if all(w in hay or (w.endswith("s") and w[:-1] in hay) for w in words):
                hits.append(s)
    return _page(request, "search.html", f"Search: {qs} | {BRAND['short']}" if qs else f"Search | {BRAND['short']}",
                 "Search procedures by name or CPT code.", "/search", nav="procedures", noindex=True,
                 crumbs=[_home_crumb(), {"name": "Search"}], hits=hits, search_q=qs,
                 hit_base=f"/{area}/procedure/" if area else "/procedure/")


@app.get("/hospitals")
def hospitals_redirect():
    _consumer_only()
    return RedirectResponse(f"/{MARKETS[DEFAULT_STATE]['slug']}/hospitals", status_code=301)


def _state_hub(request, st):
    hosp = _state_hospitals(st)
    metros = [dict(m, n=len(STATE["metros"]["ids"].get(m["key"], ())))
              for m in STATE["metros"]["list"] if m["state"] == st]
    name = MARKETS[st]["name"]
    return _page(request, "state_hub.html", f"{name} medical prices: hospitals and cash-pay providers | {BRAND['short']}",
                 f"Compare published hospital and cash prices in {name}: {sum(1 for h in hosp if h['codes'])} "
                 f"hospitals with prices, by procedure, city, and category. No paid placement.",
                 f"/{MARKETS[st]['slug']}", nav="hub",
                 crumbs=[_home_crumb(), {"name": name}], st=st, state_name=name,
                 state_slug=MARKETS[st]["slug"], hosp=hosp, metros=metros,
                 n_priced=sum(1 for h in hosp if h["codes"]), indep=_freestanding_list(st),
                 top=[s for s in TOP_PROCS], popular=STATE.get("popular", []), state_path=_state_path(st),
                 **_hub_extra(st, None))


def _metro_hub(request, st, mk):
    m = next(x for x in STATE["metros"]["list"] if x["key"] == mk)
    hosp = [h for h in _state_hospitals(st) if h["metro"] == mk]
    city = m["city"].split(",")[0]
    return _page(request, "metro_hub.html", f"{city} medical prices: hospitals and cash-pay options | {BRAND['short']}",
                 f"Published prices at {len(hosp)} {m['label']} hospitals plus independent cash-pay providers, "
                 f"by procedure. Compare, then negotiate. No paid placement.",
                 f"/{MARKETS[st]['slug']}/{mk}", nav="hub",
                 crumbs=[_home_crumb(), _state_crumb(st), {"name": m["label"]}],
                 st=st, state_name=MARKETS[st]["name"], state_slug=MARKETS[st]["slug"], m=m, city=city,
                 hosp=hosp, n_priced=sum(1 for h in hosp if h["codes"]), indep=_freestanding_list(st, mk),
                 top=TOP_PROCS, popular=STATE.get("popular", []), **_hub_extra(st, mk))


def _hub_extra(st, mk):
    """Category tiles + cash-pay listings for a state or metro hub."""
    return {"tiles": _tiles(st, mk), "cash_providers": _cash_providers(st, mk)}


def _cash_in_cat(st, mk, cat):
    groups = set(hubs.CASH_GROUPS.get(cat, []))
    return cashpay.rank([r for r in CASH["rows"] if r["group"] in groups and r["price"] is not None
                         and r["provider"]["state"] == st and (mk is None or r["provider"]["metro"] in (mk, None))])


def _tiles(st, mk=None):
    """One tile per care category: typical hospital price for its anchor code, facility count,
    and the lowest published cash price in the category (hospital or independent)."""
    base = f"/{MARKETS[st]['slug']}" + (f"/{mk}" if mk else "")
    out = []
    for slug, label, blurb, rep, codes in hubs.CATEGORIES:
        d = _result(rep, st, mk) if rep in _by_code else None
        if not d:
            continue
        sm = hubs.summarize(d)
        cash = _cash_in_cat(st, mk, slug)
        lows = [x for x in (sm["cash_low"], cash[0]["price"] if cash else None) if x is not None]
        out.append({"slug": slug, "label": label, "blurb": blurb, "code": rep, "desc": _by_code[rep]["desc"],
                    "typical": sm["median"], "n_fac": sm["n_fac"] + len({r["provider"]["slug"] for r in cash}),
                    "cash_low": min(lows) if lows else None, "url": f"{base}/{slug}",
                    "n_codes": sum(1 for c in codes if c in _by_code)})
    return out


def _hospital_index(request, st):
    hosp = _state_hospitals(st)
    name = MARKETS[st]["name"]
    return _page(request, "hospitals.html", f"{name} hospitals and facilities with published prices | {BRAND['short']}",
                 f"Every {name} hospital we track, how many procedures each publishes a usable price for, "
                 f"plus independent cash-pay facilities.", f"/{MARKETS[st]['slug']}/hospitals", nav="hospitals",
                 crumbs=[_home_crumb(), _state_crumb(st), {"name": "Hospitals"}], st=st, state_name=name,
                 hosp=hosp, n_priced=sum(1 for h in hosp if h["codes"]), indep=_freestanding_list(st),
                 cash_providers=_hub_extra(st, None)["cash_providers"])


def _live_state_by_slug(slug):
    st = MARKET_BY_SLUG.get(slug)
    if not st:
        return None, None
    return st, (None if st == _market(st) else _coming_soon(st))


# ---------- Cash-pay directory + facility profiles ----------
import threading  # noqa: E402
import cashpay  # noqa: E402

CASH = cashpay.load(os.environ.get("CASHPAY_SEED") or os.path.join(CFG, "cashpay", "seed_providers.json"))
FREESTANDING_SLUG = {}          # slug -> {name, type, city, metro, state, prices{code: price}}
for _code, _facs in STATE["freestanding"].items():
    for _f in _facs:
        _e = FREESTANDING_SLUG.setdefault(hubs.slugify(_f["name"]), dict(_f, prices={}))
        _e["prices"][_code] = _f["price"]


def _cash_url(p):
    return f"/{MARKETS.get(p['state'], MARKETS[DEFAULT_STATE])['slug']}/{p['city_slug']}/facilities/{p['slug']}"


def _freestanding_url(name):
    e = FREESTANDING_SLUG.get(hubs.slugify(name))
    if not e or e["state"] not in MARKETS:
        return ""
    return f"/{MARKETS[e['state']]['slug']}/{e.get('metro') or 'phoenix'}/facilities/{hubs.slugify(name)}"


def fac_url(h):
    """Profile link for a compare-table row (consumer only): hospitals by id, independents by name."""
    if not CONSUMER:
        return ""
    return _hosp_url(h["id"]) if h.get("id") else _freestanding_url(h.get("name") or "")


templates.env.globals["fac_url"] = fac_url
templates.env.globals["cash_url"] = _cash_url


def _cash_providers(st, metro=None):
    return [p for p in CASH["providers"] if p["state"] == st and (metro is None or p["metro"] in (metro, None))]


_CAT, _CAT_LOCK, _RES = {}, threading.Lock(), {}


def _result(code, st, mk=None):
    """One code's compare result (the analyze() dict) for a state or metro, memoized per data
    stamp. Tiles and category hubs only need a handful of codes, so they call this instead of
    building a whole catalog: on a slow disk one analyze() can take ~2 s (laptop preview)."""
    key = (_cache_stamp(), code, st, mk)
    if key in _RES:
        return _RES[key]
    rows = [r for r in _analyze(code, st, None, mk) if r["resolved"]]
    d = rows[0]["result"] if rows else None
    if d is not None and not (d.get("hospitals") or d.get("neg_median") is not None):
        d = None
    if len(_RES) > 20000:
        _RES.clear()
    _RES[key] = d
    return d


def _catalog(st, mk=None):
    """{code: analyze() result} for every tracked code in a state (or metro), per data stamp.
    Hospital profiles need all of it; it IS the compare computation, so they agree with
    the compare tables. Warmed at start-up (see _warm)."""
    key = (_cache_stamp(), st, mk)
    hit = _CAT.get(key)
    if hit is not None:
        return hit
    with _CAT_LOCK:
        hit = _CAT.get(key)
        if hit is not None:
            return hit
        out = {}
        for s in STATE["services"]:
            d = _result(s["code"], st, mk)
            if d is not None:
                out[s["code"]] = d
        for k in [k for k in _CAT if k[0] != key[0]]:
            del _CAT[k]
        if out:
            _CAT[key] = out
        return out


_HOSP_RANGE = {}


def _hosp_ranges(hid):
    """code -> (negotiated min, max) for one hospital, straight from its file rows."""
    stamp = os.path.getmtime(RATES_DIR) if os.path.isdir(RATES_DIR) else 0
    if (stamp, hid) in _HOSP_RANGE:
        return _HOSP_RANGE[(stamp, hid)]
    from pricing import rates_source
    from pricing import _has_parquet
    out = {}
    if _has_parquet(RATES_DIR):
        out = {c: (lo, hi) for c, lo, hi in _con().execute(
            f"SELECT code, min(negotiated_min), max(negotiated_max) FROM {rates_source(RATES_DIR)} "
            f"WHERE hospital_id = ? GROUP BY 1", [hid]).fetchall()}
    if len(_HOSP_RANGE) > 500:
        _HOSP_RANGE.clear()
    _HOSP_RANGE[(stamp, hid)] = out
    return out


@app.get("/cash", response_class=HTMLResponse)
def cash_directory(request: Request, type: str = "", city: str = "", procedure: str = ""):
    """Filterable directory of independent cash-pay prices. Ordered by price, then data quality.
    Nobody pays to be here and nothing else touches the order."""
    _consumer_only()
    f = cashpay.facets(CASH)
    type_ = type if type in dict(f["types"]) else ""
    city = city if city in dict(f["cities"]) else ""
    group = procedure if procedure in dict(f["groups"]) else ""
    rows = cashpay.filter_rows(CASH, type_, city, group)
    provs = [p for p in CASH["providers"] if (not type_ or p["type"] == type_) and (not city or p["city_slug"] == city)
             and (not group or group in p["groups"])]
    label = " ".join(x for x in (dict(f["groups"]).get(group, ""), dict(f["types"]).get(type_, ""),
                                 ("in " + dict(f["cities"])[city]) if city else "") if x)
    filtered = bool(type_ or city or group)
    return _page(request, "cash.html",
                 f"{label + ' c' if label else 'C'}ash prices{'' if city else ' in Arizona'} from independent providers | {BRAND['short']}",
                 "Self-pay prices that Arizona imaging centers, labs, endoscopy centers, and direct primary care "
                 "practices publish themselves, with the source and date for every price. No paid placement.",
                 "/cash", nav="cash", noindex=filtered,
                 crumbs=[_home_crumb(), _state_crumb(DEFAULT_STATE), {"name": "Cash prices"}],
                 facets=f, sel={"type": type_, "city": city, "procedure": group}, rows=rows, provs=provs,
                 meta=CASH["meta"], label=label, filtered=filtered)


def _cash_profile(request, st, place, slug):
    p = CASH["by_slug"].get(slug)
    if p and p["state"] == st and p["city_slug"] == place:
        rows = cashpay.rank(p["rows"])
        return _page(request, "facility.html", f"{p['name']} cash prices | {BRAND['short']}",
                     f"{p['name']} ({p['city']}, {p['state']}): {p['n_priced']} published self-pay prices with "
                     f"source and date.", _cash_url(p), nav="facility",
                     crumbs=[_home_crumb(), _state_crumb(st), {"name": "Cash prices", "url": "/cash"}, {"name": p["name"]}],
                     p=p, rows=rows, kind="cash")
    e = FREESTANDING_SLUG.get(slug)
    if e and e["state"] == st and (e.get("metro") or "phoenix") == place:
        items = [{"svc": _by_code[c], "code": c, "price": v, "medicare": (_result(c, st) or {}).get("medicare_rate"),
                  "url": f"/{place}/procedure/{c}"} for c, v in sorted(e["prices"].items(), key=lambda kv: kv[1])
                 if c in _by_code]
        return _page(request, "facility.html", f"{e['name']} prices | {BRAND['short']}",
                     f"{e['name']}: published cash prices for {len(items)} tracked procedures, compared with "
                     f"Medicare.", _freestanding_url(e["name"]), nav="facility",
                     crumbs=[_home_crumb(), _state_crumb(st), {"name": "Hospitals", "url": f"/{MARKETS[st]['slug']}/hospitals"},
                             {"name": e["name"]}], e=e, items=items, kind="freestanding")
    raise HTTPException(status_code=404)


@app.get("/{state_slug}/{place}/facilities/{slug}", response_class=HTMLResponse)
def facility_profile(request: Request, state_slug: str, place: str, slug: str):
    _consumer_only()
    st, soon = _live_state_by_slug(state_slug)
    if not st or soon:
        raise HTTPException(status_code=404)
    return _cash_profile(request, st, place, slug)


@app.get("/{state_slug}/{metro}/hospitals/{slug}", response_class=HTMLResponse)
def hospital_profile(request: Request, state_slug: str, metro: str, slug: str):
    """One hospital: every tracked code it prices, with the same figures as the compare tables."""
    _consumer_only()
    st, soon = _live_state_by_slug(state_slug)
    hid = next((h for h, s in HOSP_STATE.items() if s == st and _hosp_slug(h) == slug), None) if st and not soon else None
    if not hid or STATE["metros"]["of"].get(hid) != metro:
        raise HTTPException(status_code=404)
    cat, ranges = _catalog(st), _hosp_ranges(hid)
    items = []
    for code, d in cat.items():
        h = next((x for x in d.get("hospitals") or [] if x.get("id") == hid), None)
        if not h:
            continue
        lo, hi = ranges.get(code, (None, None))
        mc = d.get("medicare_rate")
        items.append({"svc": _by_code[code], "code": code, "cash": h.get("cash"), "median": h.get("median"),
                      "price": h.get("price"), "rate_type": h.get("rate_type"), "outlier": h.get("outlier"),
                      "neg_lo": lo, "neg_hi": hi, "medicare": mc,
                      "xm": (h["price"] / mc) if (mc and h.get("price")) else None,
                      "cat": hubs.category_of(code), "url": f"/{metro}/procedure/{code}"})
    order = {c[0]: i for i, c in enumerate(hubs.CATEGORIES)}
    items.sort(key=lambda i: (order.get(i["cat"], 99), i["svc"]["desc"]))
    name = STATE["hospital_names"].get(hid, hid)
    m = next(x for x in STATE["metros"]["list"] if x["key"] == metro)
    return _page(request, "hospital.html", f"{name} prices for {len(items)} procedures | {BRAND['short']}",
                 f"{name}: published cash and negotiated prices for {len(items)} common procedures, compared with "
                 f"Medicare and the {m['label']}.", _hosp_url(hid), nav="facility",
                 crumbs=[_home_crumb(), _state_crumb(st), {"name": m["label"], "url": f"/{MARKETS[st]['slug']}/{metro}"},
                         {"name": "Hospitals", "url": f"/{MARKETS[st]['slug']}/hospitals"}, {"name": name}],
                 hid=hid, name=name, m=m, items=items, stars=STATE["hospital_stars"].get(hid),
                 mrf=HOSP_URL.get(hid, ""), ccn=HOSP_CCN.get(hid, ""), cat_labels=dict((c[0], c[1]) for c in hubs.CATEGORIES))


# ---------- Review queue + newsletter (local SQLite only; nothing auto-publishes) ----------
import community  # noqa: E402


def _client_ip(request):
    return request.client.host if request.client else "?"


@app.post("/corrections", response_class=HTMLResponse)
async def corrections_submit(request: Request):
    """'Submit or correct a listing' -> review queue. Honeypot + per-IP rate limit."""
    _consumer_only()
    form = dict(await request.form())
    if form.get("website"):                                  # bots fill every field
        return RedirectResponse("/corrections?sent=1#form", status_code=303)
    fields, err = community.validate(form, capture.valid_email)
    if not err and not capture.allow("sub:" + _client_ip(request)):
        err = "Too many submissions from your connection. Try again in an hour."
    if err:
        return _corrections_page(request, error=err, f=form, status=400)
    sid = community.submit(fields, _client_ip(request))
    return RedirectResponse(f"/corrections?sent={sid}#form", status_code=303)


def _corrections_page(request, error="", f=None, sent="", status=200):
    blank = {k: "" for k in ("kind", "role", "facility", "city", "procedure", "price", "source_url",
                             "details", "email", "page")}
    blank.update({k: v for k, v in (f or {}).items() if k in blank and isinstance(v, str)})
    return _page(request, "corrections.html", f"Corrections & listings | {BRAND['short']}",
                 "Report a wrong price or a missing provider. Every report is reviewed by a person; "
                 "nothing is published automatically.", "/corrections",
                 crumbs=[_home_crumb(), {"name": "Corrections"}], sla=CORRECTIONS_SLA, status=status,
                 kinds=community.KINDS, roles=community.ROLES, f=blank, error=error,
                 sent=sent if str(sent).isdigit() else "")


@app.post("/newsletter", response_class=HTMLResponse)
async def newsletter(request: Request):
    """Store an email for future updates. No platform and no mail is sent yet."""
    _consumer_only()
    form = await request.form()
    back = form.get("page") or "/"
    back = back if isinstance(back, str) and back.startswith("/") and not back.startswith("//") else "/"
    if form.get("website"):
        return _page(request, "newsletter.html", "Signed up", "", "/newsletter", noindex=True, ok=True,
                     email="", back=back)
    email = capture.valid_email(form.get("email"))
    if not email:
        return _page(request, "newsletter.html", "Sign up", "", "/newsletter", noindex=True, ok=False,
                     error="That email address doesn't look right.", back=back, status=400)
    if not capture.allow("nl:" + _client_ip(request)):
        return _page(request, "newsletter.html", "Sign up", "", "/newsletter", noindex=True, ok=False,
                     error="Too many sign-ups from your connection. Try again in an hour.", back=back, status=429)
    community.signup(email, DEFAULT_STATE, back, _client_ip(request))
    return _page(request, "newsletter.html", f"You're on the list | {BRAND['short']}", "", "/newsletter",
                 noindex=True, ok=True, email=email, back=back)


# ---------- Speed: rendered-page cache, warm-up, gzip ----------
# Procedure, hub and methodology pages were taking 5-30 s on the 3.8 GB VPS (one 504). The
# data only changes at the weekly swap, so a rendered page is reusable until RATES_DIR's
# mtime changes (the swap) or PAGE_CACHE_TTL passes. Forms, search and geo lookups are never cached.
import time as _time  # noqa: E402
from collections import OrderedDict  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

_UNDER_TEST = "pytest" in sys.modules
PAGE_CACHE_TTL = int(os.environ.get("PAGE_CACHE_TTL", "0" if _UNDER_TEST else "3600"))
PAGE_CACHE_MAX = int(os.environ.get("PAGE_CACHE_MAX", "3000"))
_NO_CACHE = {"/corrections", "/search", "/newsletter", "/geo", "/map", "/refer", "/eob", "/ask", "/email", "/letter"}
_PAGE_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()


def _cache_stamp():
    """Key for the page cache and the result/catalog memos: the dataset swap (top dir mtime)
    plus the rate-table generation, so cached pages drop when the tables are rebuilt. Not
    named _data_stamp: that is the table loader's own (fuller) change detector."""
    return (os.path.getmtime(RATES_DIR) if os.path.isdir(RATES_DIR) else 0, _TBL["gen"])


@app.middleware("http")
async def page_cache(request: Request, call_next):
    path = request.url.path
    if (request.method != "GET" or PAGE_CACHE_TTL <= 0 or path in _NO_CACHE
            or path.startswith("/static") or request.headers.get("authorization")):
        return await call_next(request)
    key, now, stamp = (path, request.url.query), _time.time(), _cache_stamp()
    hit = _PAGE_CACHE.get(key)
    if hit and hit[0] == stamp and now - hit[1] < PAGE_CACHE_TTL:
        _PAGE_CACHE.move_to_end(key)
        return Response(hit[2], media_type="text/html; charset=utf-8", headers={"X-Page-Cache": "HIT"})
    resp = await call_next(request)
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("text/html"):
        return resp
    body = b"".join([chunk async for chunk in resp.body_iterator])
    _PAGE_CACHE[key] = (stamp, now, body)
    while len(_PAGE_CACHE) > PAGE_CACHE_MAX:
        _PAGE_CACHE.popitem(last=False)
    return Response(body, media_type="text/html; charset=utf-8", headers={"X-Page-Cache": "MISS"})


app.add_middleware(GZipMiddleware, minimum_size=1000)


def _warm():
    """Precompute what the guide pages read (coverage, per-hospital counts, the default
    state's catalog and its promoted metros) right after start-up, off the request path."""
    try:
        _cov()
        _hosp_stats()
        _tiles(DEFAULT_STATE)                       # home page first: 13 codes, not 107
        for m in STATE["metros"]["list"]:
            if m["state"] == DEFAULT_STATE and m["promote"]:
                _tiles(DEFAULT_STATE, m["key"])
        _catalog(DEFAULT_STATE)
        for m in STATE["metros"]["list"]:
            if m["state"] == DEFAULT_STATE and m["promote"]:
                _catalog(DEFAULT_STATE, m["key"])
    except Exception as ex:   # warm-up is an optimization; the pages compute lazily anyway
        print(f"warm-up skipped: {type(ex).__name__}: {ex}", file=sys.stderr)


if CONSUMER and os.environ.get("WARM_CATALOG", "0" if _UNDER_TEST else "1") == "1":
    threading.Thread(target=_warm, name="warm-catalog", daemon=True).start()


# ---- catch-all hub routes: keep these LAST so every fixed path above wins ----
@app.get("/{slug}", response_class=HTMLResponse)
def one_segment(request: Request, slug: str):
    """/arizona (state hub); /phoenix and other bare metro keys 301 to their canonical hub."""
    _consumer_only()
    st, soon = _live_state_by_slug(slug)
    if st:
        return soon or _state_hub(request, st)
    if slug in STATE["metros"]["ids"]:
        st = STATE["metros"]["state_of"][slug]
        return RedirectResponse(f"/{MARKETS[st]['slug']}/{slug}", status_code=301)
    raise HTTPException(status_code=404)


@app.get("/{a}/{b}", response_class=HTMLResponse)
def two_segments(request: Request, a: str, b: str):
    """/arizona/phoenix (metro hub), /arizona/hospitals, /arizona/mri (category hub);
    /phoenix/mri 301s to /arizona/phoenix/mri."""
    _consumer_only()
    st, soon = _live_state_by_slug(a)
    if st:
        if soon:
            return soon
        if b == "hospitals":
            return _hospital_index(request, st)
        if b in STATE["metros"]["ids"] and STATE["metros"]["state_of"][b] == st:
            return _metro_hub(request, st, b)
        if b in hubs.CAT_BY_SLUG:
            return _category_hub(request, st, None, b)
    elif a in STATE["metros"]["ids"] and b in hubs.CAT_BY_SLUG:
        st = STATE["metros"]["state_of"][a]
        return RedirectResponse(f"/{MARKETS[st]['slug']}/{a}/{b}", status_code=301)
    raise HTTPException(status_code=404)


def _category_hub(request, st, mk, cat):
    """/arizona/mri or /arizona/phoenix/mri: every tracked code in the family with typical,
    middle-50% range and n (same numbers as the CPT pages), the lowest published prices for
    the anchor codes, and independent cash listings for the category."""
    slug, label, blurb, rep, codes = hubs.CAT_BY_SLUG[cat]
    m = next((x for x in STATE["metros"]["list"] if x["key"] == mk), None) if mk else None
    place = m["label"] if m else MARKETS[st]["name"]
    city = m["city"].split(",")[0] if m else MARKETS[st]["name"]
    proc_base = f"/{mk}/procedure/" if mk else f"{_state_path(st)}/procedure/"
    rows = []
    for c in codes:
        d = _result(c, st, mk) if c in _by_code else None
        if not d:
            continue
        sm = hubs.summarize(d)
        sd = _result(c, st) if mk else None
        url = (f"/{MARKETS[st]['slug']}/{mk}/{CODE_TO_SLUG[c]}" if mk and c in CODE_TO_SLUG else f"{proc_base}{c}")
        rows.append({"svc": _by_code[c], "code": c, "sm": sm, "url": url,
                     "state_typical": sd.get("neg_median") if sd else None})
    if not rows:
        raise HTTPException(status_code=404)
    lowest = [r for r in rows if r["sm"]["lowest"]][:3]
    cash = _cash_in_cat(st, mk, cat)
    base = f"/{MARKETS[st]['slug']}" + (f"/{mk}" if mk else "")
    crumbs = [_home_crumb(), _state_crumb(st)]
    if m:
        crumbs.append({"name": m["label"], "url": f"/{MARKETS[st]['slug']}/{mk}"})
    crumbs.append({"name": label})
    others = [{"label": c[1], "url": f"{base}/{c[0]}"} for c in hubs.CATEGORIES if c[0] != cat]
    areas = ([{"label": f"All {MARKETS[st]['name']}", "url": f"/{MARKETS[st]['slug']}/{cat}"}] if mk else []) + \
        [{"label": x["label"], "url": f"/{MARKETS[st]['slug']}/{x['key']}/{cat}"}
         for x in STATE["metros"]["list"] if x["state"] == st and x["key"] != mk]
    return _page(request, "category.html", f"{label} cost in {city}: hospital and cash prices | {BRAND['short']}",
                 f"{label} prices in {place}: {len(rows)} procedure codes with typical price, range and facility "
                 f"count, plus the lowest published cash prices. No paid placement.",
                 f"{base}/{cat}", nav="category", crumbs=crumbs, label=label, blurb=blurb, place=place,
                 city=city, rows=rows, lowest=lowest, cash=cash[:12], n_cash=len(cash), metro=mk, st=st,
                 cash_groups=hubs.CASH_GROUPS.get(cat, []), others=others, areas=areas)
