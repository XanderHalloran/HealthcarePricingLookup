"""Defensive parser for CMS Hospital MRF v3.0.0 "tall" CSV files (and the "wide" variant).

Format reality: row 1 + row 2 are the hospital metadata block, row 3 is the data
header, data starts row 4. So `skiprows=2` (header at index 2).

Hospitals deviate from the spec constantly: column names use spaces instead of
`|`, casing varies, columns go missing, and one file mixes CPT/HCPCS/DRG/revenue
codes. We map headers by a normalized key (lowercased, runs of spaces/pipes
collapsed to one space) so `code|1|type` and `code 1 type` resolve the same, and
we never crash on a missing column — we just yield None for that field.

`parse_mrf(path)` yields MrfRow records (one per data line). Aggregation across
payer rows happens in ingest.py, not here.

WIDE layout (Intermountain, CHS, UNM, ...): no payer_name column; instead one
`standard_charge|<payer>|<plan>|negotiated_dollar` column PER payer. We detect those
columns and emit one MrfRow per non-empty payer cell (mirroring the tall/JSON shape), so
ingest is layout-agnostic. A tall file's single `standard_charge|negotiated_dollar` wins.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass


@dataclass
class MrfRow:
    description: str
    codes: list[tuple[str, str]]   # [(raw_code, declared_type), ...] from this line
    cash: float | None
    negotiated_dollar: float | None
    charge_min: float | None
    charge_max: float | None
    payer: str | None = None       # raw payer name for the negotiated_dollar row


def _key(header: str) -> str:
    """Normalize a header for comparison: lower, collapse spaces/pipes/underscores.
    So `standard_charge|discounted_cash` and `standard charge discounted cash` match."""
    return re.sub(r"[\s|_]+", " ", header.strip().lower())


def _money(val: str | None) -> float | None:
    """Parse a charge cell: strip $ , and whitespace; blank/non-numeric -> None."""
    if val is None:
        return None
    s = val.strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_CODE_RE = re.compile(r"^code (\d+)$")
_CODE_TYPE_RE = re.compile(r"^code (\d+) type$")


def _map_columns(header: list[str]) -> dict:
    """Locate the columns we care about by normalized key. Missing -> absent."""
    cols = {"codes": {}, "code_types": {}}
    for i, h in enumerate(header):
        k = _key(h)
        if (m := _CODE_RE.match(k)):
            cols["codes"][m.group(1)] = i
        elif (m := _CODE_TYPE_RE.match(k)):
            cols["code_types"][m.group(1)] = i
        elif k == "description":
            cols["description"] = i
        elif "discounted cash" in k:
            cols["cash"] = i
        elif k == "standard charge negotiated dollar":
            cols["negotiated_dollar"] = i
        elif k.startswith("standard charge ") and k.endswith(" negotiated dollar"):
            # wide: 'standard charge <payer> <plan> negotiated dollar' -> payer label
            cols.setdefault("wide", []).append((k[len("standard charge "):-len(" negotiated dollar")], i))
        elif "standard charge" in k and k.endswith("min"):
            cols["charge_min"] = i
        elif "standard charge" in k and k.endswith("max"):
            cols["charge_max"] = i
        elif "payer" in k:
            cols["payer"] = i
    return cols


def _cell(row: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def parse_mrf(src):
    """Yield MrfRow for each data line. Header at row index 2 (skiprows=2).
    `src` = a path, or an open BINARY file-like (e.g. a zip member streamed without
    extraction — the 43 GB University of Utah CSV never touches the disk)."""
    if isinstance(src, str):
        f = open(src, newline="", encoding="utf-8-sig", errors="replace")   # cp1252 descriptions happen
    else:
        f = io.TextIOWrapper(src, encoding="utf-8-sig", errors="replace", newline="")
    with f:
        reader = csv.reader(f)
        for _ in range(2):                       # skip metadata block
            next(reader, None)
        try:
            header = next(reader)
        except StopIteration:
            return                                # empty file -> no rows
        cols = _map_columns(header)

        for row in reader:
            if not any(c.strip() for c in row):   # blank line
                continue
            codes = []
            for n, ci in cols["codes"].items():
                raw = _cell(row, ci)
                if raw and raw.strip():
                    declared = _cell(row, cols["code_types"].get(n))
                    codes.append((raw.strip(), (declared or "").strip()))
            base = dict(
                description=(_cell(row, cols.get("description")) or "").strip(),
                codes=codes,
                cash=_money(_cell(row, cols.get("cash"))),
                charge_min=_money(_cell(row, cols.get("charge_min"))),
                charge_max=_money(_cell(row, cols.get("charge_max"))),
            )
            if "negotiated_dollar" in cols or not cols.get("wide"):        # tall
                yield MrfRow(negotiated_dollar=_money(_cell(row, cols.get("negotiated_dollar"))),
                             payer=(_cell(row, cols.get("payer")) or "").strip() or None, **base)
                continue
            wide = [(p, _money(_cell(row, i))) for p, i in cols["wide"]]
            wide = [(p, v) for p, v in wide if v is not None]
            if not wide:                                                   # cash/min/max only
                yield MrfRow(negotiated_dollar=None, **base)
            for p, v in wide:
                yield MrfRow(negotiated_dollar=v, payer=p, **base)
