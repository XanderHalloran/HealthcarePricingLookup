"""Billing-code normalizer.

The messiest part of the system: consumer-pasted hospital bills mix CPT, HCPCS,
DRG, and revenue codes inconsistently, often with descriptions and extra columns
on the same line. This module pulls ONE billing code out of a messy line and
classifies it, flagging anything it can't confidently resolve.

Spec (see also README "Normalization spec"):
  1. Strip surrounding whitespace, uppercase.
  2. Scan whitespace/delimiter-separated tokens left-to-right; take the FIRST
     token that matches a known code pattern. (Handles "73721 MRI..." and
     "MRI... 73721" alike.)
  3. Strip a trailing 2-char modifier (e.g. J1234-RT -> J1234), noted separately.
  4. Classify by pattern:
       \\d{5}            -> CPT  (Category I)
       \\d{4}[FT]        -> CPT  (Category II / III)
       [A-V]\\d{4}       -> HCPCS (Level II)
       0\\d{3}           -> REV  (revenue code, 4-digit, leading zero)
       \\d{1,3}          -> DRG  (MS-DRG; short bare numerics are ambiguous -> noted)
       else             -> UNKNOWN
  5. Revenue codes name a facility department, not a 1:1 service — always flagged
     (matched=False) so they surface in the dropped-codes report.

Ambiguity ceiling: a bare 3-digit number could be an MS-DRG OR a non-zero-padded
revenue code (e.g. "450"). We assume DRG and set `matched=False` with a note so
it surfaces in the dropped-codes report instead of being silently miscategorized.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

def to_float(v) -> float | None:
    """A money cell to float: strips $ and commas; blank or non-numeric -> None.
    One parser for the MRF readers, the Medicare reference loader and the web form, which all
    meet the same dollar strings in different shapes (JSON number, CSV cell, typed input)."""
    if v is None:
        return None
    try:
        return float(v)                     # already numeric (JSON), or a clean string
    except (TypeError, ValueError):
        pass
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


_MODIFIER = re.compile(r"-([A-Z0-9]{2})$")
_DELIMS = re.compile(r"[\s,|;\t]+")

_PATTERNS = [
    ("CPT",     re.compile(r"^\d{5}$")),
    ("CPT",     re.compile(r"^\d{4}[FT]$")),
    ("HCPCS",   re.compile(r"^[A-V]\d{4}$")),
    ("REV",     re.compile(r"^0\d{3}$")),
    ("DRG",     re.compile(r"^\d{1,3}$")),
]


@dataclass
class NormalizedCode:
    raw: str            # the original input line, untouched
    code: str | None    # cleaned code, or None if nothing code-like found
    code_type: str      # CPT | HCPCS | DRG | REV | UNKNOWN
    matched: bool       # True only when confidently classified & resolvable
    note: str = ""      # why it was flagged, modifier captured, mapping applied, etc.
    modifier: str = ""  # 2-char CPT modifier if the code was written CODE-MOD (e.g. 25, 59)


def _classify(token: str) -> tuple[str, str] | None:
    """Return (code_type, '') for a token matching a pattern, else None."""
    for code_type, pat in _PATTERNS:
        if pat.match(token):
            return code_type, ""
    return None


def normalize_code(line: str) -> NormalizedCode:
    """Normalize one pasted line into a single classified billing code."""
    raw = line
    cleaned = line.strip().upper()
    if not cleaned:
        return NormalizedCode(raw, None, "UNKNOWN", False, "empty line")

    for token in _DELIMS.split(cleaned):
        if not token:
            continue
        modifier = ""
        m = _MODIFIER.search(token)
        if m:
            base = token[: m.start()]
            # Only treat as a modifier if the base is itself code-like.
            if _classify(base):
                modifier = m.group(1)
                token = base
        hit = _classify(token)
        if not hit:
            continue

        code_type, _ = hit
        note_parts = []
        if modifier:
            note_parts.append(f"modifier {modifier} stripped")
        matched = True

        if code_type == "REV":
            note_parts.append("revenue code")     # facility department, not a service
            matched = False
        elif code_type == "DRG":
            # Bare numerics are ambiguous (MS-DRG vs non-zero-padded revenue code).
            note_parts.append("bare numeric assumed MS-DRG; verify")
            matched = False

        return NormalizedCode(raw, token, code_type, matched, "; ".join(note_parts), modifier)

    return NormalizedCode(raw, None, "UNKNOWN", False, "no recognizable code")


def normalize_lines(text: str) -> list[NormalizedCode]:
    """Normalize a pasted block (one code per line). Skips blank lines."""
    return [normalize_code(ln) for ln in text.splitlines() if ln.strip()]


# Strong money signals: a $, a comma-grouped figure, or a 2-decimal amount.
_AMOUNT_STRONG = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*\.\d{2}")


def extract_amounts(line: str, code: str | None = None) -> list[float]:
    """All dollar amounts on a line (distinct, largest first), ignoring the code.

    A line may carry two figures: the charge and, from an EOB, the plan-allowed
    amount. The allowed amount is always <= the charge, so callers read [0] as the
    charge and [1] (if present) as the allowed amount.
    Prefers $/decimal/comma tokens; falls back to a single bare integer.
    """
    def to_f(s):
        return float(s.replace("$", "").replace(",", "").strip())

    code_val = None
    if code:
        try:
            code_val = float(code)
        except ValueError:
            code_val = None

    strong = {v for v in (to_f(m) for m in _AMOUNT_STRONG.findall(line)) if v != code_val}
    if strong:
        return sorted(strong, reverse=True)

    ints = {float(t) for t in re.findall(r"\b\d{2,}\b", line)} - {code_val}
    return [next(iter(ints))] if len(ints) == 1 else []

