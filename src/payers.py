"""Payer-name normalizer: collapse the wildly inconsistent payer/plan names in
hospital MRFs ("AETNA HEALTH INC", "Aetna Better Health", "BCBS of AZ", "UHC",
"Optum", "AHCCCS", "Mercy Care") into a small set of canonical insurers the UI
can offer in a dropdown.

Brand keywords are checked BEFORE Medicare/Medicaid so "Aetna Medicare Advantage"
maps to Aetna (a real plan you'd shop), not generic Medicare. Anything unmatched
(self-pay, one-off TPAs) returns None and is dropped — we keep payer_rates focused
on the major insurers a consumer actually has.
"""
from __future__ import annotations

CANONICAL = ["Aetna", "Blue Cross Blue Shield", "Cigna", "UnitedHealthcare",
             "Humana", "Tricare", "Medicaid", "Medicare"]

_RULES = [
    ("Aetna", ["aetna"]),
    ("Blue Cross Blue Shield", ["blue cross", "blue shield", "bcbs", "bluecross", "anthem"]),
    ("Cigna", ["cigna", "evernorth"]),
    ("UnitedHealthcare", ["unitedhealth", "united health", "uhc", "optum", "umr"]),
    ("Humana", ["humana"]),
    ("Tricare", ["tricare"]),
    ("Medicaid", ["medicaid", "ahcccs", "mercy care", "molina"]),
    ("Medicare", ["medicare"]),
]


def normalize_payer(raw: str | None) -> str | None:
    """Map a raw payer/plan name to a canonical insurer, or None to drop it."""
    if not raw:
        return None
    s = raw.lower()
    for canon, kws in _RULES:
        if any(k in s for k in kws):
            return canon
    return None
