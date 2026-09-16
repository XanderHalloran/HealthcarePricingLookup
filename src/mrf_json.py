"""Streaming parser for CMS MRF v3.0.0 *JSON* files (the format used by
CommonSpirit/Dignity and Tenet/Abrazo, which don't publish CSV).

Yields the same MrfRow records as the CSV parser so ingest.py is format-agnostic.
Uses ijson to stream the `standard_charge_information` array — these files run to
tens/hundreds of MB and json.load() would spike memory on the shared box.

JSON shape (per item):
  { "description": ...,
    "code_information": [ {"code":"73721","type":"CPT"}, {"code":"0450","type":"RC"} ],
    "standard_charges": [
      { "minimum":.., "maximum":.., "discounted_cash":.., "gross_charge":..,
        "payers_information": [ {"payer_name":..,"standard_charge_dollar":..}, ... ] } ] }

We emit one MrfRow per payer (mirroring the tall-CSV one-row-per-payer shape) so
ingest's negotiated min/median/max aggregation works unchanged.
"""
from __future__ import annotations

import codecs

import ijson

from mrf_parser import MrfRow


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        s = str(v).replace("$", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None


_CTRL = bytes.maketrans(bytes(range(0x20)), b" " * 0x20)   # every C0 control byte -> space


class _CleanStream:
    """Binary reader that blanks raw control bytes. JSON forbids them inside strings and treats
    them as whitespace outside, so a space is always a safe substitute (HCA Chippenham ships a
    stray control character inside a description and ijson aborts the whole file)."""
    def __init__(self, f):
        self._f = f

    def read(self, n=-1):
        return self._f.read(n).translate(_CTRL)


def parse_mrf_json(src):
    """Yield MrfRow for each (code-set × charge × payer). Streams the file.
    `src` = a path or an open binary file-like (streamed zip member)."""
    with (open(src, "rb") if isinstance(src, str) else src) as f:
        if f.read(3) != codecs.BOM_UTF8:         # Epic-style exports carry a UTF-8 BOM; ijson rejects it
            f.seek(0)
        for item in ijson.items(_CleanStream(f), "standard_charge_information.item"):
            desc = (item.get("description") or "").strip()
            codes = []
            for ci in item.get("code_information") or []:
                code = str(ci.get("code", "")).strip()
                ctype = str(ci.get("type") or ci.get("code_type") or "").strip()
                if code:
                    codes.append((code, ctype))
            if not codes:
                continue
            for sc in item.get("standard_charges") or []:
                cash = _num(sc.get("discounted_cash"))
                cmin, cmax = _num(sc.get("minimum")), _num(sc.get("maximum"))
                payers = sc.get("payers_information") or sc.get("payers") or []
                if payers:
                    for p in payers:
                        yield MrfRow(desc, codes, cash,
                                     _num(p.get("standard_charge_dollar")), cmin, cmax,
                                     (p.get("payer_name") or "").strip() or None)
                else:                          # cash/min/max only, no payer rows
                    yield MrfRow(desc, codes, cash, None, cmin, cmax)
