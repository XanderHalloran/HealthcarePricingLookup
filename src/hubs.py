"""Consumer price-guide structure: care categories, URL slugs, and the precomputed catalog.

Pure data + helpers; app.py owns the routes. A category is a hand-picked list of tracked
codes (not a keyword guess) so a hub never shows a code that belongs elsewhere.

Canonical URL scheme (one per page type; the other spellings 301 here):
  /arizona                          state hub
  /arizona/phoenix                  metro hub              (/phoenix 301s here)
  /arizona/mri, /arizona/phoenix/mri  category hubs        (/phoenix/mri 301s here)
  /arizona/hospitals                facility index         (/hospitals 301s here)
  /arizona/phoenix/hospitals/{slug} hospital profile
  /arizona/{city}/facilities/{slug} independent facility profile
Procedure pages keep their existing URLs; the readable slug URL stays rel=canonical
(tests pin that /phoenix/procedure/70551 still answers 200).
"""
from __future__ import annotations

import re
import statistics

# slug, label, short blurb, representative code (for tiles), member codes in display order
CATEGORIES = [
    ("mri", "MRI", "Brain, spine and joint MRI scans", "73721",
     ["73721", "72148", "70551", "70553", "72141", "73221"]),
    ("ct", "CT scan", "Head, chest, spine and abdomen CT", "74177",
     ["74177", "74176", "74178", "70450", "71250", "71260", "72125", "72131", "70486"]),
    ("x-ray", "X-ray", "Chest, spine and joint X-rays", "71046",
     ["71046", "71045", "72110", "73562", "73610", "73130"]),
    ("ultrasound", "Ultrasound", "Abdominal, pelvic, vascular and OB ultrasound", "76700",
     ["76700", "76705", "76856", "76830", "93880", "76805", "76801"]),
    ("mammography", "Mammogram & bone density", "Screening and diagnostic mammograms, DEXA", "77067",
     ["77067", "77066", "77065", "77080"]),
    ("gi-endoscopy", "Colonoscopy & endoscopy", "Screening and diagnostic scopes", "45378",
     ["45378", "G0121", "45380", "45385", "43235", "43239"]),
    ("labs", "Lab tests", "Blood panels, A1c, thyroid, urinalysis", "80053",
     ["80053", "80048", "85025", "85027", "80061", "83036", "84443", "81001", "80050",
      "84153", "80076", "85610", "86140", "82306", "87635", "88305"]),
    ("er", "Emergency room", "ER visit levels 2 through 5", "99284",
     ["99282", "99283", "99284", "99285"]),
    ("office-visits", "Office visits", "New, established and preventive visits", "99213",
     ["99213", "99214", "99215", "99203", "99204", "99205", "99385", "99386", "99395",
      "99396", "G0463"]),
    ("surgery", "Outpatient surgery", "Cataract, hernia, gallbladder, joint and more", "66984",
     ["66984", "29881", "47562", "49650", "49505", "44970", "64721", "29827", "27447",
      "27130", "52000", "55250", "42820", "10060"]),
    ("heart", "Heart tests", "EKG, echo, stress test, catheterization", "93306",
     ["93000", "93306", "93016", "93458"]),
    ("therapy", "Therapy & injections", "Physical therapy, injections, infusions, sleep study", "97110",
     ["97110", "97140", "97162", "20610", "62323", "96372", "96365", "95810"]),
    ("maternity", "Pregnancy & women's health", "Delivery, OB ultrasound, gynecology", "59400",
     ["59400", "59510", "76801", "58571", "58120", "57454"]),
]
CAT_BY_SLUG = {c[0]: c for c in CATEGORIES}
# Category slugs must never equal a procedure slug (allowlist `slug:`), because both live at
# /arizona/phoenix/<slug>: hence 'mammography' (vs the 'mammogram' page) and 'gi-endoscopy'.
# Hub category -> cash-pay directory groups (src/cashpay.py GROUPS) shown on that hub.
CASH_GROUPS = {"mri": ["mri"], "ct": ["ct"], "x-ray": ["x-ray"], "ultrasound": ["ultrasound"],
               "mammography": ["mammogram", "dexa"], "gi-endoscopy": ["colonoscopy"], "labs": ["labs"],
               "office-visits": ["primary-care"]}

# Everyday words people search with that the official descriptions don't contain.
ALIASES = {
    "73721": "knee ankle hip leg", "73221": "shoulder elbow wrist arm", "72148": "back lower back",
    "72141": "neck", "70551": "head", "70553": "head", "70450": "head", "71250": "lung",
    "71046": "lung", "73562": "knee", "29881": "knee meniscus", "27447": "knee", "27130": "hip",
    "29827": "shoulder", "47562": "gallbladder", "77067": "breast", "77066": "breast", "77065": "breast",
    "77080": "bone density osteoporosis", "45378": "colon", "G0121": "colon", "80053": "blood test",
    "85025": "blood test", "80061": "cholesterol blood test", "83036": "diabetes blood sugar",
    "59400": "birth baby pregnancy", "59510": "c-section birth baby", "66984": "eye",
    "93000": "ecg heart", "93306": "heart echo", "76700": "sonogram", "76856": "sonogram",
    "99284": "emergency", "99283": "emergency", "99285": "emergency", "99213": "doctor visit",
}


def category_of(code):
    for c in CATEGORIES:
        if code in c[4]:
            return c[0]
    return None


def slugify(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def summarize(result):
    """The few numbers a hub tile / table row needs from one analyze() result."""
    facs = result.get("hospitals") or []
    cash = sorted(h["cash"] for h in facs if h.get("cash") is not None)
    return {
        "median": result.get("neg_median"), "low": result.get("neg_low"), "high": result.get("neg_high"),
        "n_range": result.get("n_hospitals") or 0, "n_fac": len(facs),
        "n_hosp": sum(1 for h in facs if (h.get("type") or "hospital") == "hospital"),
        "cash_median": statistics.median(cash) if cash else None, "n_cash": len(cash),
        "cash_low": cash[0] if cash else None,
        "medicare": result.get("medicare_rate"),
        "lowest": sorted((h for h in facs if h.get("price") is not None and not h.get("outlier")),
                         key=lambda h: h["price"])[:5],
    }
