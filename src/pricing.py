"""Pricing lookup + negotiation coaching.

lookup() pulls the Medicare benchmark (the floor) and the local hospital cash /
negotiated range for one code+state. target_ask() computes the suggested ask.
render_points() fills the config templates with the numbers. analyze() ties the
normalizer + lookup + coaching together for a pasted block of codes (Phase 4 calls
this).

Missing data is reported, never silently omitted — a gap ("no Medicare rate",
"no local cash price") is itself useful negotiation information.
"""
from __future__ import annotations

import math
import os
import statistics

import duckdb
import yaml

from ingest import rates_source
from normalize import normalize_lines, extract_amounts

# Which Medicare source is the right benchmark per code type. For a HOSPITAL bill
# the facility-outpatient rate (OPPS) is the relevant anchor; PFS is the fallback.
SOURCE_PRIORITY = {
    "CPT": ["OPPS", "PFS", "CLFS"],     # facility outpatient first, then PFS, then labs (CLFS)
    "HCPCS": ["OPPS", "PFS", "CLFS"],
    "DRG": ["IPPS"],
}


def _fmt(x):
    return None if x is None else f"${x:,.2f}"


def get_medicare(con, code, code_type):
    """Best Medicare rate for (code, code_type) by source priority. -> (rate, source)."""
    rows = con.execute(
        "SELECT source, rate FROM medicare_rates WHERE code=? AND code_type=?",
        [code, code_type],
    ).fetchall()
    by_source = {s: r for s, r in rows}
    for src in SOURCE_PRIORITY.get(code_type, []):
        if src in by_source:
            return by_source[src], src
    # fall back to any source we have
    if by_source:
        s = next(iter(by_source))
        return by_source[s], s
    return None, None



def _has_parquet(rates_dir):
    return os.path.isdir(rates_dir) and any(
        f.endswith(".parquet") for _, _, fs in os.walk(rates_dir) for f in fs
    )


def get_hospital_breakdown(con, rates_dir, code, state, payer=None, payer_dir=None):
    """Per-hospital prices for (code, state), cheapest first — the Bluebook-style
    facility list. When `payer` is given, each hospital's price is THAT insurer's
    negotiated rate (from payer_rates); otherwise cash/self-pay where published,
    else the negotiated median."""
    if payer and payer_dir and _has_parquet(payer_dir):
        rows = con.execute(
            f"""SELECT hospital_id, TRY_CAST(negotiated_rate AS DOUBLE) r
                FROM {rates_source(payer_dir)} WHERE code=? AND state=? AND payer=?
                ORDER BY r NULLS LAST""",
            [code, state.upper(), payer],
        ).fetchall()
        return [{"hospital_id": h, "cash": None, "median": r, "price": r, "rate_type": payer}
                for h, r in rows if r is not None]
    if not _has_parquet(rates_dir):
        return []
    rows = con.execute(
        f"""SELECT hospital_id, TRY_CAST(cash_price AS DOUBLE), TRY_CAST(negotiated_median AS DOUBLE)
            FROM {rates_source(rates_dir)} WHERE code=? AND state=?
            ORDER BY coalesce(TRY_CAST(cash_price AS DOUBLE), TRY_CAST(negotiated_median AS DOUBLE)) NULLS LAST""",
        [code, state.upper()],
    ).fetchall()
    out = []
    for hid, cash, med in rows:
        price = cash if cash is not None else med
        if price is None:
            continue
        out.append({"hospital_id": hid, "cash": cash, "median": med, "price": price,
                    "rate_type": "cash" if cash is not None else "negotiated"})
    return out


def load_hospital_names(hospitals_path):
    """id -> friendly hospital name, from the manifest (falls back to id)."""
    with open(hospitals_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {h["id"]: h.get("name", h["id"]) for h in data.get("hospitals", [])}


def load_quality(quality_path):
    """id -> CMS overall star rating (1-5) or None, from config/quality.yaml."""
    with open(quality_path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("ratings", {})


def load_hip_knee(path):
    """id -> {rate, compare} from Care Compare COMP_HIP_KNEE (hip/knee replacement
    complication rate), under `hip_knee:` in quality.yaml. Empty for configs without it."""
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("hip_knee") or {}


def get_medicare_all(con, code, code_type):
    """Every Medicare benchmark we hold for a code: {source: rate} (OPPS, PFS, ASC, IPPS...)."""
    return {s: r for s, r in con.execute(
        "SELECT source, rate FROM medicare_rates WHERE code=? AND code_type=?", [code, code_type]).fetchall()}


def payer_matrix(con, rates_dir, payer_dir, code, state, names=None, metro_ids=None, payers_allowed=None):
    """Facility x payer negotiated-rate matrix for one code (the contracting benchmark).
    Returns {'payers': [...], 'rows': [{id,name,cash,median,rates:{payer:rate}}],
             'stats': {column: {n,min,q1,median,q3,max}}} — columns = 'cash','median' + each payer."""
    from payers import CANONICAL
    names = names or {}
    rows = {}
    for h in get_hospital_breakdown(con, rates_dir, code, state):
        if metro_ids is not None and h["hospital_id"] not in metro_ids:
            continue
        rows[h["hospital_id"]] = {"id": h["hospital_id"], "name": names.get(h["hospital_id"], h["hospital_id"]),
                                  "cash": h["cash"], "median": h["median"], "rates": {}}
    if _has_parquet(payer_dir):
        for hid, payer, rate in con.execute(
                f"""SELECT hospital_id, payer, TRY_CAST(negotiated_rate AS DOUBLE)
                    FROM {rates_source(payer_dir)} WHERE code=? AND state=?""", [code, state.upper()]).fetchall():
            if rate is None or (metro_ids is not None and hid not in metro_ids):
                continue
            rows.setdefault(hid, {"id": hid, "name": names.get(hid, hid), "cash": None, "median": None, "rates": {}})
            rows[hid]["rates"][payer] = rate
    payers = [p for p in (payers_allowed or CANONICAL) if any(p in r["rates"] for r in rows.values())]
    stats = {}
    for col in ["cash", "median"] + payers:
        vals = sorted(v for v in ((r["rates"].get(col) if col in payers else r.get(col)) for r in rows.values()) if v is not None)
        if vals:
            q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else [vals[0], statistics.median(vals), vals[-1]]
            stats[col] = {"n": len(vals), "min": vals[0], "q1": q[0], "median": statistics.median(vals),
                          "q3": q[2], "max": vals[-1]}
    # name breaks the tie: `rows` is keyed by hospital id and filled in query order, which is
    # not stable between processes, so without it a shared /rates link listed the facilities
    # that share a median in a different order on someone else's screen.
    out_rows = sorted(rows.values(), key=lambda r: (r["median"] is None, r["median"] or 0, r["name"]))
    return {"payers": payers, "rows": out_rows, "stats": stats}


def load_metros(path):
    """Metro grouping + state registry from config/metros.yaml. Returns
    {'list': [{key,label,city,state,promote}], 'ids': {key: set(hospital_id)},
     'of': {id: key}, 'state_of': {key: state}, 'states': [{code,name,slug}]}."""
    if not os.path.exists(path):
        return {"list": [], "ids": {}, "of": {}, "state_of": {}, "states": []}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    lst, ids, of, state_of = [], {}, {}, {}
    for m in data.get("metros", []):
        s = set(m.get("ids", []))
        ids[m["key"]] = s
        for i in s:
            of[i] = m["key"]
        st = str(m.get("state", "AZ")).upper()
        state_of[m["key"]] = st
        lst.append({"key": m["key"], "label": m["label"], "state": st,
                    "promote": bool(m.get("promote")),
                    "city": m.get("slug_city", m["label"])})
    states = [{"code": str(s["code"]).upper(), "name": s["name"], "slug": s["slug"]}
              for s in data.get("states", [])] or [{"code": "AZ", "name": "Arizona", "slug": "arizona"}]
    return {"list": lst, "ids": ids, "of": of, "state_of": state_of, "states": states}


def load_freestanding(path):
    """code -> [{name, type, city, price}] of freestanding imaging/ASC cash prices."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    idx = {}
    for fac in data.get("facilities", []):
        for code, price in (fac.get("prices") or {}).items():
            idx.setdefault(str(code), []).append(
                {"name": fac["name"], "type": fac.get("type", "imaging"),
                 "city": fac.get("city", ""), "price": float(price),
                 "state": str(fac.get("state", "AZ")).upper(),  # current freestanding are all AZ
                 "metro": fac.get("metro", "phoenix")})
    return idx


def load_geo(path):
    """hospital_id -> (lat, lon) for distance sorting. Approximate (city-level)
    coordinates; good enough to rank facilities by how far away they are."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = {}
    for hid, ll in (data.get("hospitals") or {}).items():
        if ll and len(ll) == 2:
            out[hid] = (float(ll[0]), float(ll[1]))
    return out


def load_contact(path):
    """hospital_id -> {address, city, zip, phone}. Referral staff need something to hand a
    patient; a price with no address is why they stay in a maps app. Empty when absent."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {hid: {k: (v.get(k) or "") for k in ("address", "city", "zip", "phone")}
            for hid, v in (data.get("hospitals") or {}).items() if v}


# City centroids for freestanding imaging/ASC (which carry a city, not an id).
CITY_COORDS = {
    "Phoenix": (33.4484, -112.0740), "Scottsdale": (33.4942, -111.9261),
    "Mesa": (33.4152, -111.8315), "Tempe": (33.4255, -111.9400),
    "Gilbert": (33.3528, -111.7890), "Chandler": (33.3062, -111.8413),
    "Glendale": (33.5387, -112.1860), "Peoria": (33.5806, -112.2374),
    "Tucson": (32.2226, -110.9747),
}


def haversine(a, b):
    """Great-circle distance in miles between (lat,lon) tuples a and b."""
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return round(3958.8 * 2 * math.asin(math.sqrt(h)), 1)


def price_stats(values, medicare=None):
    """Robust price distribution for the facility prices of one code.
    Returns the IQR band (q1..q3), a median that EXCLUDES implausible outliers, the
    true min/max, and per-value outlier flags. Outlier = below a hard floor
    (0.25× Medicare — a $20 MRI is a mis-keyed file, not a deal) or outside the
    Tukey 1.5×IQR fences. Returns None if there are no prices."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    floor = medicare * 0.25 if medicare else 0
    if len(xs) >= 4:
        q1, q3 = statistics.quantiles(xs, n=4)[0], statistics.quantiles(xs, n=4)[2]
        iqr = q3 - q1
        lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    else:
        q1, q3, lo_fence, hi_fence = xs[0], xs[-1], -math.inf, math.inf

    def flag(v):   # 'low' | 'high' | None
        if v is None:
            return None
        if v < floor or v < lo_fence:
            return "low"
        if v > hi_fence:
            return "high"
        return None

    kept = [v for v in xs if flag(v) is None] or xs
    return {"min": xs[0], "max": xs[-1], "q1": q1, "q3": q3,
            "median": statistics.median(kept), "n": len(xs), "n_used": len(kept),
            "n_excluded": len(xs) - len(kept), "flag": flag}


def target_ask(medicare_rate, cash_price, multiplier):
    """Lower of (medicare * multiplier) or the local cash price. -> (amount, basis)."""
    candidates = []
    if medicare_rate is not None:
        candidates.append((round(medicare_rate * multiplier, 2), "medicare"))
    if cash_price is not None:
        candidates.append((cash_price, "cash"))
    if not candidates:
        return None, None
    return min(candidates, key=lambda c: c[0])


def lookup(con, rates_dir, code, code_type, state, description="", multiplier=2.0,
           hospital_names=None, hospital_stars=None, payer_dir=None, payer=None,
           freestanding=None, metro=None, metro_ids=None, user_loc=None, geo=None,
           hip_knee=None):
    """Assemble the full pricing picture for one code. Flags missing pieces.
    `payer` reprices the facility list to one insurer; `metro`/`metro_ids` restrict it to
    one metro (apples-to-apples). The range is a ROBUST distribution (price_stats): the
    q1..q3 IQR band, a median that EXCLUDES mis-keyed outliers, and the true min/max; each
    facility carries an `outlier` flag. `user_loc` (lat,lon) + `geo` (id->coords) add a
    per-facility distance. Freestanding imaging/ASC options are appended (never flagged)."""
    med_rate, med_source = get_medicare(con, code, code_type)
    names = hospital_names or {}
    stars = hospital_stars or {}
    hk = hip_knee or {}
    breakdown = [{"id": h["hospital_id"],
                  "name": names.get(h["hospital_id"], h["hospital_id"]),
                  "stars": stars.get(h["hospital_id"]), "hip_knee": hk.get(h["hospital_id"]),
                  "cash": h["cash"], "median": h["median"], "price": h["price"],
                  "rate_type": h["rate_type"], "type": "hospital"}
                 for h in get_hospital_breakdown(con, rates_dir, code, state, payer, payer_dir)]
    if metro_ids is not None:                   # restrict to one metro for apples-to-apples
        breakdown = [b for b in breakdown if b["id"] in metro_ids]

    # Robust distribution over the per-facility prices — the median excludes mis-keyed
    # outliers (a $20 MRI), and the band is the q1..q3 IQR instead of the raw min..max.
    st = price_stats([b["price"] for b in breakdown], med_rate)
    if st:
        neg_low, neg_med, neg_high = st["q1"], st["median"], st["q3"]
        neg_min, neg_max, n_hosp, n_excl = st["min"], st["max"], st["n"], st["n_excluded"]
        for b in breakdown:
            b["outlier"] = st["flag"](b["price"])
    else:
        neg_low = neg_med = neg_high = neg_min = neg_max = None
        n_hosp = n_excl = 0

    cashes = [b["cash"] for b in breakdown if b.get("cash") is not None]
    cash = statistics.median(cashes) if cashes else None
    ask, basis = target_ask(med_rate, cash, multiplier)

    # Freestanding imaging/ASC cash options (curated — never flagged as outliers).
    for fs in (freestanding or {}).get(code, []):
        if fs.get("state", "AZ") != state.upper():
            continue                            # a Phoenix imaging center isn't a Utah option
        if metro and fs.get("metro") and fs["metro"] != metro:
            continue                            # ...nor a Tucson one
        breakdown.append({"id": None, "name": fs["name"], "stars": None, "cash": fs["price"],
                          "median": None, "price": fs["price"], "rate_type": "cash",
                          "type": fs["type"], "city": fs.get("city", ""), "outlier": None,
                          "source": fs.get("source", "")})

    # Per-facility distance (hospitals by id, freestanding by city centroid).
    if user_loc and geo:
        for b in breakdown:
            coords = geo.get(b["id"]) if b["id"] else CITY_COORDS.get(b.get("city"))
            b["distance"] = haversine(user_loc, coords)

    priced = [b for b in breakdown if b["price"] is not None]
    if priced:
        # name breaks the tie here too: when several facilities share the lowest price, which
        # one wore the 'lowest' badge otherwise changed between processes
        min(priced, key=lambda b: (b["price"], b["name"]))["cheapest"] = True   # survives re-sort
    # name breaks the tie: many facilities share one payer's median, and without a total
    # order the row order varied between identical requests -- so the same lookup ranked a
    # different facility first on each refresh and a shared link showed two different answers.
    breakdown.sort(key=lambda b: (b["price"] if b["price"] is not None else 1e12, b["name"]))
    return {
        "code": code, "code_type": code_type, "state": state.upper(),
        "description": description, "payer": payer, "metro": metro,
        "medicare_rate": med_rate, "medicare_source": med_source,
        "cash_price": cash,
        "neg_low": neg_low, "neg_median": neg_med, "neg_high": neg_high,
        "neg_min": neg_min, "neg_max": neg_max,
        "n_hospitals": n_hosp, "n_excluded": n_excl,
        "target_ask": ask, "target_basis": basis,
        "hospitals": breakdown,
        "medicare_missing": med_rate is None,
        "hospital_missing": not breakdown,
    }


def load_templates(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["talking_points"]


def render_points(result, templates):
    """Render talking points from a lookup result. Returns list of dicts
    {id, text, missing}. Missing required fields -> the template's missing_note."""
    money = {"medicare_rate", "cash_price", "neg_low", "neg_median", "neg_high",
             "target_ask", "your_price", "overcharge"}
    ctx = {}
    for k, v in result.items():
        ctx[k] = _fmt(v) if (k in money and v is not None) else v
    safe = {k: ("—" if v is None else v) for k, v in ctx.items()}

    points = []
    for t in templates:
        have = all(result.get(f) is not None for f in t.get("requires", []))
        if have:
            points.append({"id": t["id"], "text": t["text"].format_map(safe).strip(),
                           "missing": False})
        elif t.get("missing_note"):
            points.append({"id": t["id"],
                           "text": t["missing_note"].format_map(safe).strip(),
                           "missing": True})
    return points


def analyze(con, rates_dir, pasted_text, state, templates, descriptions=None,
            multiplier=2.0, hospital_names=None, hospital_stars=None, payer_dir=None,
            payer=None, freestanding=None, metro=None, metro_ids=None,
            user_loc=None, geo=None, hip_knee=None):
    """Full path for a pasted block: normalize -> lookup -> coach, per code.
    Returns a list of {input, code, code_type, resolved, result, points, note}."""
    descriptions = descriptions or {}
    out = []
    for nc in normalize_lines(pasted_text):
        if nc.code is None or nc.code_type not in ("CPT", "HCPCS", "DRG"):
            out.append({"input": nc.raw, "code": nc.code, "code_type": nc.code_type,
                        "resolved": False, "note": nc.note or "unrecognized code",
                        "result": None, "points": []})
            continue
        desc = descriptions.get((nc.code, nc.code_type), "")
        res = lookup(con, rates_dir, nc.code, nc.code_type, state, desc, multiplier,
                     hospital_names, hospital_stars, payer_dir, payer, freestanding,
                     metro, metro_ids, user_loc, geo, hip_knee)
        amounts = extract_amounts(nc.raw, nc.code)        # [charge, allowed?]
        amount = amounts[0] if amounts else None
        res["your_price"] = amount
        res["allowed"] = amounts[1] if len(amounts) >= 2 else None
        ask = res["target_ask"]
        res["overcharge"] = (round(amount - ask, 2)
                             if (amount is not None and ask is not None) else None)
        out.append({"input": nc.raw, "code": nc.code, "code_type": nc.code_type,
                    "modifier": nc.modifier, "resolved": True, "note": nc.note,
                    "result": res, "points": render_points(res, templates)})
    return out


def load_services(allowlist_path):
    """Ordered list of {code, type, desc, category} for the 'shop a price' picker.
    Descriptions for labelling derive from this: {(s['code'], s['type']): s['desc']}."""
    with open(allowlist_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [{"code": str(e["code"]).upper(), "type": str(e["type"]).upper(),
             "desc": e.get("desc", ""), "category": e.get("category", "other"),
             "slug": e.get("slug")}          # top shoppable procedures get a URL slug (SEO pages)
            for e in data["codes"]]
