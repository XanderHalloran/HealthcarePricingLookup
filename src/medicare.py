"""Loaders for the small, clean Medicare reference files into medicare_rates.

Three sources, three formats, three rate calculations:
  PFS  (Physician Fee Schedule RVU file): rate = total_RVU * conversion_factor.
       National, GPCI=1.0. ponytail: locality adjustment (rate = sum(RVU*GPCI)*CF)
       is the upgrade path when locality matters; the RVU file already has the
       components, only the per-locality GPCI table is missing.
  OPPS (Addendum B): the national unadjusted payment rate is given directly.
  IPPS (MS-DRG weights): rate = relative_weight * ipps_base_operating_rate.

Each loader is defensive about column names (same normalized-key trick as the MRF
parser) and writes into the medicare_rates table via the passed DuckDB connection.
"""
from __future__ import annotations

import csv
import os
import re

import duckdb

from normalize import normalize_code


def _key(h: str) -> str:
    return re.sub(r"[\s|_]+", " ", h.strip().lower())


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _find(header, *needles):
    """First column index whose normalized key contains ALL needles, else None."""
    for i, h in enumerate(header):
        k = _key(h)
        if all(n in k for n in needles):
            return i
    return None


def _clean_code(raw: str) -> str | None:
    n = normalize_code(raw)
    return n.code


def build_reference_db(db_path: str, schema_sql: str):
    """Create the reference tables from schema.sql in a DuckDB database file."""
    con = duckdb.connect(db_path)
    with open(schema_sql, encoding="utf-8") as f:
        con.execute(f.read())
    return con


def build_reference(con, root: str, cfg: dict) -> dict:
    """Load PFS/OPPS/IPPS/CLFS into medicare_rates from the paths in medicare.yaml.
    Missing files are skipped (the app still runs). Returns per-source row counts."""
    year = cfg.get("year", 2024)
    cf = cfg.get("pfs_conversion_factor", 32.74)
    src = cfg.get("sources", {})
    counts = {}
    loaders = {
        "pfs": lambda p: load_pfs(con, p, year, cf),
        "opps": lambda p: load_opps(con, p, year),
        "clfs": lambda p: load_clfs(con, p, year),
        "ipps": lambda p: load_ipps(con, p),
        "asc": lambda p: load_asc(con, p),
    }
    for name, loader in loaders.items():
        path = src.get(name)
        full = os.path.join(root, path) if path else None
        counts[name] = loader(full) if full and os.path.exists(full) else 0
    return counts


def _rows_from_csv(path, header_cell=None):
    """Yield (header, row) for each data row. Real CMS files prepend title rows,
    so when header_cell is given we skip until a row contains a cell exactly equal
    to it (e.g. 'HCPCS Code' for OPPS); otherwise the first row is the header."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if header is None:
                if header_cell is None or any((c or "").strip() == header_cell for c in row):
                    header = row
                continue
            if any(c.strip() for c in row):
                yield header, row


def _insert(con, rows):
    if not rows:                      # duckdb executemany rejects an empty parameter list
        return
    con.executemany(
        """INSERT OR REPLACE INTO medicare_rates
           (code, code_type, locality, rate, year, source) VALUES (?,?,?,?,?,?)""",
        rows,
    )


def load_pfs(con, path, year, conversion_factor, locality="NATIONAL"):
    """PFS PPRRVU file -> rate = NON-FACILITY TOTAL RVU * conversion_factor.

    The PPRRVU CSV has title rows then a TWO-ROW header (CMS splits column names
    across two lines, e.g. 'NON-FACILITY' above 'TOTAL'). We find the row whose
    first cell is 'HCPCS' and combine it with the row above to reconstruct full
    names. The non-facility total is the all-in office rate — a reasonable consumer
    benchmark. ponytail: non-facility total; labs (status X, paid under the Clinical
    Lab Fee Schedule) carry 0 RVU here and are skipped -> no PFS rate for them.
    """
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    hi = next((i for i, r in enumerate(rows) if r and r[0].strip() == "HCPCS"), None)
    if hi is None:
        return 0
    above = rows[hi - 1] if hi > 0 else []
    combined = [f"{(above[j] if j < len(above) else '').strip()} {c.strip()}".strip()
                for j, c in enumerate(rows[hi])]
    ci = _find(combined, "hcpcs")
    ci = ci if ci is not None else 0
    ti = _find(combined, "non-facility", "total")
    if ti is None:
        return 0
    inserts = []
    for r in rows[hi + 1:]:
        if not any(x.strip() for x in r):
            continue
        code = _clean_code(r[ci]) if ci < len(r) else None
        total = _num(r[ti]) if ti < len(r) else None
        if not code or not total:                 # skip 0/empty RVU (non-payable, labs)
            continue
        ctype = "HCPCS" if code[0].isalpha() else "CPT"
        inserts.append((code, ctype, locality, round(total * conversion_factor, 2), year, "PFS"))
    _insert(con, inserts)
    return len(inserts)


def load_opps(con, path, year, locality="NATIONAL"):
    """OPPS Addendum B -> national unadjusted payment rate, given directly."""
    inserts = []
    first = True
    ci = ri = None
    for header, row in _rows_from_csv(path, header_cell="HCPCS Code"):
        if first:
            ci = _find(header, "hcpcs") or _find(header, "code") or 0
            ri = _find(header, "payment", "rate") or _find(header, "payment")
            first = False
        code = _clean_code(row[ci])
        rate = _num(row[ri]) if ri is not None else None
        if not code or rate is None:
            continue
        ctype = "HCPCS" if code[0].isalpha() else "CPT"
        inserts.append((code, ctype, locality, rate, year, "OPPS"))
    _insert(con, inserts)
    return len(inserts)


def load_ipps(con, path, locality="NATIONAL"):
    """IPPS from a small YAML (config-ortho/ipps.yaml): rate = DRG relative weight x
    (operating + capital national base rate). ponytail: hand-listed MSK DRGs from CMS
    Table 5 rather than parsing the full table; wage-index 1.0."""
    import yaml
    with open(path, encoding="utf-8") as f:
        d = (yaml.safe_load(f) or {}).get("ipps") or {}
    base = float(d.get("operating_base_rate", 0)) + float(d.get("capital_base_rate", 0))
    year = int(d.get("fiscal_year", 2026))
    inserts = [(str(code), "DRG", locality, round(float(w) * base, 2), year, "IPPS")
               for code, w in (d.get("weights") or {}).items() if w]
    _insert(con, inserts)
    return len(inserts)


def load_asc(con, path, locality="NATIONAL"):
    """Medicare ASC fee schedule (Addendum AA national rate) from YAML: {code: rate}. The
    hospital-outpatient (OPPS) vs ASC pair for one CPT is the site-of-care differential."""
    import yaml
    with open(path, encoding="utf-8") as f:
        d = (yaml.safe_load(f) or {}).get("asc") or {}
    year = int(d.get("year", 2025))
    inserts = []
    for code, rate in (d.get("rates") or {}).items():
        if rate is None:
            continue
        code = str(code).upper()
        inserts.append((code, "HCPCS" if code[0].isalpha() else "CPT", locality, float(rate), year, "ASC"))
    _insert(con, inserts)
    return len(inserts)


def load_clfs(con, path, year, locality="NATIONAL"):
    """Clinical Laboratory Fee Schedule -> national payment rate, given directly.
    This is the Medicare benchmark for lab tests (CMP, CBC, lipid, A1c, etc.), which
    OPPS/PFS don't price. Header row has cell 'HCPCS'; RATE column is the dollar fee.
    Duplicate code rows (base + QW modifier) carry the same rate; INSERT OR REPLACE
    dedupes them on the primary key."""
    inserts = []
    first = True
    ci = ri = None
    for header, row in _rows_from_csv(path, header_cell="HCPCS"):
        if first:
            ci = _find(header, "hcpcs")
            ci = ci if ci is not None else 0
            ri = _find(header, "rate")
            first = False
        if ri is None:
            break
        code = _clean_code(row[ci]) if ci < len(row) else None
        rate = _num(row[ri]) if ri < len(row) else None
        if not code or rate is None:
            continue
        ctype = "HCPCS" if code[0].isalpha() else "CPT"
        inserts.append((code, ctype, locality, rate, year, "CLFS"))
    _insert(con, inserts)
    return len(inserts)


