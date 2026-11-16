"""Independent cash-pay directory: Arizona providers that post self-pay prices themselves.

Reads config/cashpay/seed_providers.json (CASHPAY_SEED overrides). Pure data, no web code.
Hard rules (see config/cashpay/README.md): no paid placement, so ordering is ONLY price
then data quality; every price carries its source and dates; MDsave = prepaid bundle;
providers are never merged by address.
"""
from __future__ import annotations

import datetime
import json
import os
import re

TYPE_LABELS = {
    "independent_imaging": "Imaging center", "lab": "Lab", "direct_primary_care": "Direct primary care",
    "endoscopy_center": "Endoscopy center", "cash_pay_program": "Cash-pay program",
}
# Seed `metro` text -> metros.yaml key. Anything statewide maps to None (shown in every AZ area).
METRO_KEYS = {"Phoenix": "phoenix", "Tucson": "tucson",
              "Southern AZ (not Phoenix/Tucson metro core)": "southern",
              "Southern AZ (Tucson-adjacent)": "southern"}
# What a patient would call it -> directory filter. First match wins.
GROUPS = [
    ("mri", "MRI", r"\bMRI\b"), ("ct", "CT scan", r"\bCT\b|CT angiography"),
    ("mammogram", "Mammogram", r"mammogra"), ("x-ray", "X-ray", r"x-ray"),
    ("ultrasound", "Ultrasound", r"ultrasound|doppler|sonogra"), ("dexa", "Bone density / DEXA", r"dexa|densitometry"),
    ("biopsy", "Biopsy", r"biops|\bFNA\b"),
    ("colonoscopy", "Colonoscopy & endoscopy", r"colonoscopy|endoscop|\bEGD\b|ERCP|manometry|feeding tube"),
    ("labs", "Lab tests", r"\blab\b|panel|\bCBC\b|\bCMP\b|\bA1c\b|lipid|thyroid|\bTSH\b|vitamin|testosterone|\bPSA\b|metabolic|blood|profile|uric|insulin|ferritin|cortisol|prolactin|estradiol|progesterone|celiac|pylori"),
    ("primary-care", "Primary care visit / membership", r"visit|membership|month|annual|enroll|primary care|\bplans?\b|access care|registration|consult"),
]
_CODE_RE = re.compile(r"\b(\d{5}|G\d{4})\b")
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def slugify(s):
    s = (s or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _doc_date(url, notes):
    """The source document's own date when it shows one: a WordPress /uploads/YYYY/MM/ path,
    or 'dated Feb 2025' in the notes. None when the source carries no date of its own."""
    m = re.search(r"/uploads/(20\d\d)/(\d\d)/", url or "")
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{_MON[int(m.group(2)) - 1]} {m.group(1)}"
    m = re.search(r"dated (?:[A-Za-z]+ )?((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 20\d\d)", notes or "")
    return m.group(1) if m else None


def _match(cpt, code):
    """How precisely a seed row's CPT field names `code`: 0 exact, 1 typical/assumed, 2 family rate."""
    txt = (cpt or "").strip()
    if txt == code:
        return 0
    if "varies" in txt or "e.g." in txt or len(set(_CODE_RE.findall(txt))) > 1 or "+" in txt:
        return 2
    return 1


def _fmt_date(iso):
    try:
        d = datetime.date.fromisoformat(iso)
        return f"{d:%b} {d.day}, {d.year}"
    except (TypeError, ValueError):
        return iso or ""


def _group(name):
    for slug, _label, rx in GROUPS:
        if re.search(rx, name or "", re.I):
            return slug
    return "other"


def load(path):
    """-> {"providers": [...], "by_slug": {...}, "rows": [...], "meta": {...}}; empty when no seed."""
    empty = {"providers": [], "by_slug": {}, "rows": [], "meta": {}}
    if not path or not os.path.exists(path):
        return empty
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    providers, by_slug, rows = [], {}, []
    for p in data.get("providers", []):
        slug = base = slugify(p["provider_name"])
        n = 2
        while slug in by_slug:                       # never merge two providers, even same name/address
            slug, n = f"{base}-{n}", n + 1
        prov = {
            "slug": slug, "name": p["provider_name"], "chain": p.get("chain") or "",
            "type": p.get("type") or "", "type_label": TYPE_LABELS.get(p.get("type"), "Independent"),
            "city": p.get("city") or "", "city_slug": slugify(p.get("city")), "state": p.get("state") or "AZ",
            "address": p.get("address") or "", "zip": p.get("zip") or "", "metro_text": p.get("metro") or "",
            "metro": METRO_KEYS.get(p.get("metro")), "price_page": p.get("price_page_url") or "",
            "notes": p.get("notes") or "", "rows": [],
        }
        for q in p.get("procedures", []):
            price = q.get("cash_price")
            price = float(price) if price not in (None, "") else None
            status = q.get("price_status") or ("published" if price is not None else "call_for_price")
            src_type = p.get("source_type") or ""
            row = {
                "provider": prov, "name": q.get("procedure_name") or "", "cpt": q.get("cpt") or "",
                "codes": sorted(set(_CODE_RE.findall(q.get("cpt") or ""))),
                "price": price if status != "call_for_price" else None, "status": status,
                "from": status == "from_price", "includes": q.get("includes") or "",
                "notes": q.get("price_notes") or "", "source_url": q.get("source_url") or prov["price_page"],
                "checked": q.get("date_checked") or "", "checked_fmt": _fmt_date(q.get("date_checked")),
                "doc_date": _doc_date(q.get("source_url") or prov["price_page"], q.get("price_notes")),
                "kind": "bundle" if src_type == "mdsave" else "cash",
                "kind_label": ("Prepaid bundle (MDsave)" if src_type == "mdsave" else
                               "Cash program rate" if src_type == "cash_program" else "Published cash price"),
                "group": _group(q.get("procedure_name")),
            }
            # data quality: older source document or a 'from' price ranks below an equal price
            row["quality"] = (1 if row["doc_date"] else 0) + (1 if row["from"] else 0)
            prov["rows"].append(row)
            rows.append(row)
        prov["n_priced"] = sum(1 for r in prov["rows"] if r["price"] is not None)
        prov["low"] = min((r["price"] for r in prov["rows"] if r["price"] is not None), default=None)
        prov["groups"] = sorted({r["group"] for r in prov["rows"]})
        by_slug[slug] = prov
        providers.append(prov)
    meta = {k: data.get(k) for k in ("generated", "disclaimer", "counts")}
    return {"providers": providers, "by_slug": by_slug, "rows": rows, "meta": meta}


def rank(rows, code=None):
    """Price first, then data quality (exact code > typical > family rate; dated/'from' last).
    Rows with no number (call for price) go last. Nothing else may enter this key."""
    return sorted(rows, key=lambda r: (r["price"] is None, r["price"] or 0,
                                       _match(r["cpt"], code) if code else 0, r["quality"],
                                       r["provider"]["name"]))


def for_code(data, code, state="AZ", metro=None):
    """Seed rows that name `code` in their CPT field, for one state (and metro + statewide)."""
    out = []
    for r in data["rows"]:
        p = r["provider"]
        if code not in r["codes"] or p["state"] != state:
            continue
        if metro and p["metro"] not in (metro, None):
            continue
        out.append(dict(r, match=_match(r["cpt"], code)))
    return rank(out, code)


def filter_rows(data, type_="", city="", group=""):
    rows = [r for r in data["rows"]
            if (not type_ or r["provider"]["type"] == type_)
            and (not city or r["provider"]["city_slug"] == city)
            and (not group or r["group"] == group)]
    return rank(rows)


def facets(data):
    types, cities, groups = {}, {}, {}
    for r in data["rows"]:
        p = r["provider"]
        types[p["type"]] = p["type_label"]
        cities[p["city_slug"]] = p["city"]
        groups[r["group"]] = next((g[1] for g in GROUPS if g[0] == r["group"]), "Other")
    return {"types": sorted(types.items(), key=lambda x: x[1]),
            "cities": sorted(cities.items(), key=lambda x: x[1]),
            "groups": [(g[0], g[1]) for g in GROUPS if g[0] in groups] + ([("other", "Other")] if "other" in groups else [])}
