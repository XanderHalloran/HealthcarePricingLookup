"""Bill-analysis helpers used by the 'Analyze my bill' flow:
  - detect_issues(): high-confidence billing-error / patient-right flags
  - build_letter(): a ready-to-send negotiation/appeal letter from the priced rows

Both operate on the rows returned by pricing.analyze() (each: code, code_type,
resolved, result, ...). No new data — pure logic on what we already computed.
"""
from __future__ import annotations

from collections import Counter

ER_CODES = {"99281", "99282", "99283", "99284", "99285"}
HIGH_EM = {"99285", "99215", "99205", "99284"}   # top E&M levels — common upcoding targets

# Preventive screenings the ACA requires most plans to cover at $0.
PREVENTIVE = {"77067", "45378", "G0121", "99385", "99386", "99395", "99396"}

# Modifiers payers heavily audit (commonly misused to unbundle/re-bill).
MOD_NOTES = {
    "25": "Modifier 25 ({codes}) bills a separate office/E&M visit on the same day as a "
          "procedure. It's heavily audited — confirm the visit was significant and separate, "
          "not the routine work of the procedure.",
    "59": "Modifier 59 ({codes}) is the 'distinct procedural service' modifier — the classic "
          "way bundled codes get billed separately. Confirm the services were truly independent.",
    "91": "Modifier 91 ({codes}) bills a repeat lab test. Confirm the repeat was clinically "
          "needed, not a re-bill of the same draw.",
}

# Unbundling / overlap: if codes from set A AND set B both appear, the bill likely
# double-counts a service that's already included in another. Curated to our
# allowlist; conservative (only well-known inclusive relationships).
BUNDLE_RULES = [
    ({"80053"}, {"80048"},
     "A comprehensive metabolic panel (80053) already includes the basic metabolic "
     "panel (80048). Billing both is likely unbundling."),
    ({"85025"}, {"85027"},
     "A CBC with differential (85025) already includes a CBC without differential "
     "(85027). Billing both is likely unbundling."),
    ({"70553"}, {"70551"},
     "MRI brain with-and-without contrast (70553) includes the without-contrast study "
     "(70551). Billing both is duplicative."),
    ({"74178"}, {"74176", "74177"},
     "CT abdomen/pelvis with-and-without (74178) includes the separate with/without "
     "studies (74176/74177). Billing them in addition is duplicative."),
    ({"45380", "45385"}, {"45378"},
     "A colonoscopy with biopsy or polyp removal (45380/45385) bundles the diagnostic "
     "colonoscopy (45378) — 45378 shouldn't be billed separately."),
    ({"43239"}, {"43235"},
     "An upper endoscopy with biopsy (43239) bundles the diagnostic endoscopy (43235) "
     "— 43235 shouldn't be billed separately."),
    ({"74176"}, {"74177"},
     "CT abdomen/pelvis billed both without (74176) and with (74177) contrast — these "
     "are normally the single combined code 74178. Ask why both were billed."),
    ({"49505"}, {"49650"},
     "Both an open (49505) and laparoscopic (49650) inguinal hernia repair are billed "
     "— usually one approach per hernia. Ask which was performed."),
    ({"77067"}, {"77065", "77066"},
     "Both a screening (77067) and diagnostic (77065/77066) mammogram are billed — "
     "confirm both were done and the screening wasn't re-billed as diagnostic."),
]


def _money(v):
    return f"${v:,.0f}" if v is not None else "—"


def bill_total(rows):
    """Sum of charged amounts across resolved rows (0 if none provided)."""
    return sum(r["result"].get("your_price") or 0
               for r in rows if r.get("resolved"))


def savings_summary(rows):
    """Whole-bill roll-up: total charged vs total target vs potential savings.
    Per line the target is the suggested ask (else the local fair price), capped at
    the charged amount so we never claim a line 'saves' by being priced above charge.
    Returns None when no charges were entered."""
    charged = target = n = 0
    for r in rows:
        if not r.get("resolved"):
            continue
        d = r["result"]
        yp = d.get("your_price")
        if yp is None:
            continue
        n += 1
        charged += yp
        t = d.get("target_ask")
        if t is None:
            t = d.get("neg_median")
        target += min(t, yp) if t is not None else yp
    if not charged:
        return None
    return {"charged": round(charged), "target": round(target),
            "savings": round(max(0, charged - target)), "n": n}


def detect_issues(rows, gfe=None):
    """Return a list of {level, title, detail} flags for a pasted bill.
    Conservative: only flags we can stand behind from the data on hand.
    gfe = the patient's Good-Faith-Estimate total, if provided."""
    issues = []
    resolved = [r for r in rows if r.get("resolved")]
    codes = [r["code"] for r in resolved]

    if gfe:
        total = bill_total(rows)
        if total and total >= gfe + 400:
            issues.append({"level": "warn", "title": "Bill exceeds your Good-Faith-Estimate",
                           "detail": f"Your charges total {_money(total)} — {_money(total - gfe)} "
                                     f"over your Good-Faith-Estimate of {_money(gfe)}. Because it's "
                                     "$400+ above the estimate, you can dispute it through the federal "
                                     "Patient-Provider Dispute Resolution (PPDR) process. File within "
                                     "120 days of the bill at cms.gov (search 'patient-provider dispute "
                                     "resolution')."})

    dups = sorted({c for c, n in Counter(codes).items() if n > 1})
    if dups:
        issues.append({"level": "warn", "title": "Possible duplicate charges",
                       "detail": f"{', '.join(dups)} appear more than once. Duplicate line "
                                 "items are a common billing error — ask for each to be justified."})

    for r in resolved:
        d = r["result"]
        yp, hi = d.get("your_price"), d.get("neg_high")
        if yp is not None and hi is not None and yp > hi * 1.5:
            issues.append({"level": "warn",
                           "title": f"{r['code']}: charged above the local range",
                           "detail": f"You were charged {_money(yp)}, above the local high of "
                                     f"{_money(hi)}. That looks like the gross/list price — ask for "
                                     "the cash or negotiated rate instead."})

    if any(c in ER_CODES for c in codes):
        issues.append({"level": "info", "title": "No Surprises Act protection (ER)",
                       "detail": "For emergency care you cannot be balance-billed beyond your "
                                 "in-network cost-sharing, even out-of-network (No Surprises Act, "
                                 "2022). Dispute any balance bill above that."})

    hi_em = sorted({c for c in codes if c in HIGH_EM})
    if hi_em:
        issues.append({"level": "info", "title": "Highest-level visit codes present",
                       "detail": f"{', '.join(hi_em)} are top E&M levels. Confirm the documented "
                                 "complexity justifies the level — downcoding is a common appeal."})

    cset = set(codes)
    for a, b, msg in BUNDLE_RULES:
        if (cset & a) and (cset & b):
            issues.append({"level": "warn", "title": "Possible unbundling / overlapping codes",
                           "detail": msg})

    prev = sorted({r["code"] for r in resolved
                   if r["code"] in PREVENTIVE and (r["result"].get("your_price") or 0) > 0})
    if prev:
        issues.append({"level": "info", "title": "Preventive care should be $0",
                       "detail": f"{', '.join(prev)} are preventive screenings the ACA requires "
                                 "most plans to cover at no cost. If you were charged, they may have "
                                 "been coded as diagnostic — ask your insurer to reprocess as preventive."})

    for mod, tmpl in MOD_NOTES.items():
        codes_m = sorted({r["code"] for r in resolved if r.get("modifier") == mod})
        if codes_m:
            issues.append({"level": "warn", "title": f"Modifier {mod} — audited",
                           "detail": tmpl.format(codes=", ".join(codes_m))})

    for r in resolved:
        d = r["result"]
        charged, allowed = d.get("your_price"), d.get("allowed")
        if charged is not None and allowed is not None and charged > allowed + 1:
            issues.append({"level": "warn", "title": f"{r['code']}: possible balance bill",
                           "detail": f"You were charged {_money(charged)} but the plan-allowed "
                                     f"amount is {_money(allowed)}. For IN-NETWORK care you owe only "
                                     f"your cost-share of the {_money(allowed)} — the "
                                     f"{_money(charged - allowed)} difference can't be balance-billed "
                                     "and should be disputed."})
    return issues


def build_letter(rows, state="AZ", gfe=None):
    """Assemble a negotiation/appeal letter from the priced rows."""
    resolved = [r for r in rows if r.get("resolved")]
    out = [
        "To: Hospital Billing Department",
        "Re: Request for review and adjustment of charges",
        "",
        "To whom it may concern,",
        "",
        f"I am requesting a review and adjustment of the following charges, compared against "
        f"Medicare's published rates and local hospital prices for the {state} market:",
        "",
    ]
    for r in resolved:
        d = r["result"]
        seg = [f"- {r['code']} ({d.get('description') or r['code_type']}):"]
        if d.get("your_price") is not None:
            seg.append(f"I was charged {_money(d['your_price'])}.")
        if not d.get("medicare_missing"):
            seg.append(f"Medicare benchmark {_money(d['medicare_rate'])}.")
        if d.get("neg_median") is not None:
            seg.append(f"Local fair price {_money(d['neg_median'])}.")
        if d.get("target_ask") is not None:
            seg.append(f"I request adjustment to {_money(d['target_ask'])}.")
        out.append(" ".join(seg))
    out += [
        "",
        "Under the federal Hospital Price Transparency rule (45 CFR 180), these standard charges "
        "are public, and I am asking that my charges be brought in line with them.",
        "",
    ]
    if gfe and bill_total(rows) >= gfe + 400:
        over = bill_total(rows) - gfe
        out += [
            f"This bill of {_money(bill_total(rows))} also exceeds the Good-Faith-Estimate of "
            f"{_money(gfe)} I was provided by {_money(over)}. Under the No Surprises Act I may "
            "dispute charges that exceed my Good-Faith-Estimate by $400 or more, and I will pursue "
            "the Patient-Provider Dispute Resolution process if this is not resolved.",
            "",
        ]
    out += [
        "Please also send a fully itemized bill and information on your financial assistance / "
        "charity care policy and my eligibility (required of nonprofit hospitals under 26 U.S.C. "
        "501(r)).",
        "",
        "These figures are good-faith estimates from public data, not a statement of what I owe.",
        "",
        "Thank you for your review.",
        "",
        "Sincerely,",
        "[Your name]  /  [Account number]",
    ]
    return "\n".join(out)
