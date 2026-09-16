"""Per-hospital MRF ingest: parse -> normalize -> filter to allowlist -> aggregate
-> append to partitioned Parquet. Dropped/unmatched codes go to a report so we can
tune the allowlist and normalizer.

State and hospital_id are passed in by the caller, NOT scraped from the MRF
metadata: state extraction from free-text hospital_address is unreliable, and the
operator already knows which file belongs to which hospital. (Flagged assumption.)
"""
from __future__ import annotations

import csv
import os
import statistics
import tempfile
from collections import defaultdict

import duckdb

from mrf_parser import parse_mrf
from mrf_json import parse_mrf_json
from normalize import normalize_code
from payers import normalize_payer

# Map an MRF's declared code-type string -> our canonical type. When the MRF
# declares a type we TRUST it (so an unambiguous DRG isn't dropped by the
# consumer-paste ambiguity heuristic in normalize_code).
DECLARED_MAP = {
    "CPT": "CPT", "CPT4": "CPT", "HCPCS": "HCPCS", "CPT/HCPCS": "CPT",
    "MS-DRG": "DRG", "MSDRG": "DRG", "DRG": "DRG", "APR-DRG": "DRG", "APRDRG": "DRG",
    "RC": "REV", "REVENUE": "REV", "REVENUE CODE": "REV",
}

PARQUET_COLUMNS = [
    "hospital_id", "state", "code", "code_type",
    "cash_price", "negotiated_min", "negotiated_median", "negotiated_max",
]

# payer_rates dataset: per (hospital, code, canonical payer) negotiated rate.
PAYER_COLUMNS = ["hospital_id", "state", "code", "code_type", "payer", "negotiated_rate"]


def rates_source(out_dir: str) -> str:
    """SQL expression for the partitioned dataset. Forces state/code to VARCHAR so
    a numeric-only partition isn't read back as INTEGER (codes are always strings).
    Use everywhere the dataset is queried (Phase 3 lookups + tests)."""
    glob = out_dir.replace("\\", "/").rstrip("/") + "/**/*.parquet"
    return (f"read_parquet('{glob}', hive_partitioning=true, "
            f"hive_types={{'state':'VARCHAR','code':'VARCHAR'}})")


def load_allowlist(path: str) -> set[tuple[str, str]]:
    """Return the set of (code, code_type) we keep. Reads config/allowlist.yaml."""
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {(str(e["code"]).upper(), str(e["type"]).upper()) for e in data["codes"]}


def _resolve(raw_code: str, declared_type: str):
    """Clean a raw code and decide its CANONICAL type. Returns (code, code_type, reason).

    CPT and HCPCS Level I overlap: hospitals label the same 5-digit code as either
    "CPT" or "HCPCS" inconsistently (Banner says CPT, Valleywise says HCPCS for
    73721). So we type by code STRUCTURE via normalize_code — 5-digit -> CPT,
    letter+4 -> HCPCS — NOT the declared label. The declared label is trusted only
    for DRG, where a bare number is otherwise ambiguous.
    """
    norm = normalize_code(raw_code)
    if norm.code is None:
        return None, None, f"unparseable ({norm.note})"
    declared = DECLARED_MAP.get(declared_type.upper().strip()) if declared_type else None
    if declared == "DRG":
        return norm.code, "DRG", ""                 # trust declared DRG (bare number is ambiguous)
    ctype = norm.code_type                            # structural: CPT vs HCPCS by shape
    if ctype not in {"CPT", "HCPCS"}:
        return None, None, f"unsupported type {ctype} ({norm.note})"
    if not norm.matched:
        return None, None, f"ambiguous ({norm.note})"
    return norm.code, ctype, ""


def ingest_mrf(path, hospital_id, state, allowlist_path, out_dir, report_dir,
               payer_out_dir=None, stream=None):
    """Ingest one hospital MRF file. Returns a summary dict.
    If payer_out_dir is given, also writes a per-(code, payer) payer_rates dataset.
    `stream` = an open binary file-like to parse instead of opening `path` (whose
    extension still picks the parser) — used to read zip members without extracting."""
    allow = load_allowlist(allowlist_path)
    state = state.upper()

    # (code, code_type) -> aggregation accumulator
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"cash": None, "negs": [], "cmin": None, "cmax": None}
    )
    # (code, code_type, canonical_payer) -> list of negotiated dollars
    pagg: dict[tuple[str, str, str], list] = defaultdict(list)
    dropped: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "reason": ""})
    kept_rows = 0

    parser = parse_mrf_json if path.lower().endswith(".json") else parse_mrf
    for row in parser(stream if stream is not None else path):
        if "do not use" in (row.description or "").lower():     # deprecated chargemaster items (CORE, others)
            continue
        for raw_code, declared_type in row.codes:
            code, ctype, reason = _resolve(raw_code, declared_type)
            if reason:
                key = (raw_code.upper(), declared_type or "?")
                dropped[key]["count"] += 1
                dropped[key]["reason"] = reason
                continue
            if (code, ctype) not in allow:
                key = (code, ctype)
                dropped[key]["count"] += 1
                dropped[key]["reason"] = "not_in_allowlist"
                continue

            a = agg[(code, ctype)]
            kept_rows += 1
            if a["cash"] is None and row.cash is not None:
                a["cash"] = row.cash
            if row.negotiated_dollar is not None:
                a["negs"].append(row.negotiated_dollar)
                if payer_out_dir:
                    cp = normalize_payer(row.payer)
                    if cp:
                        pagg[(code, ctype, cp)].append(row.negotiated_dollar)
            for fld, val in (("cmin", row.charge_min), ("cmax", row.charge_max)):
                if val is not None:
                    cur = a[fld]
                    a[fld] = val if cur is None else (min(cur, val) if fld == "cmin" else max(cur, val))

    rows_out = _finalize(agg, hospital_id, state)
    if rows_out:
        _write_parquet(rows_out, out_dir, PARQUET_COLUMNS)
    if payer_out_dir and pagg:
        _write_parquet(_finalize_payers(pagg, hospital_id, state), payer_out_dir, PAYER_COLUMNS)
    _write_report(dropped, report_dir, hospital_id)

    return {
        "hospital_id": hospital_id, "state": state,
        "codes_kept": len(rows_out), "rows_kept": kept_rows,
        "distinct_dropped": len(dropped),
        "dropped_total": sum(d["count"] for d in dropped.values()),
    }


def _finalize(agg, hospital_id, state):
    rows = []
    for (code, ctype), a in agg.items():
        negs = a["negs"]
        neg_min = min(negs) if negs else a["cmin"]
        neg_med = statistics.median(negs) if negs else None
        neg_max = max(negs) if negs else a["cmax"]
        rows.append({
            "hospital_id": hospital_id, "state": state, "code": code, "code_type": ctype,
            "cash_price": a["cash"],
            "negotiated_min": neg_min, "negotiated_median": neg_med, "negotiated_max": neg_max,
        })
    return rows


def _finalize_payers(pagg, hospital_id, state):
    """Median negotiated rate per (code, canonical payer) for this hospital."""
    return [{"hospital_id": hospital_id, "state": state, "code": code, "code_type": ctype,
             "payer": payer, "negotiated_rate": round(statistics.median(negs), 2)}
            for (code, ctype, payer), negs in pagg.items() if negs]


def _write_parquet(rows, out_dir, columns):
    """Append rows to a partitioned Parquet dataset (by state then code).

    Stage to a temp CSV and let DuckDB COPY ... PARTITION_BY do the write — one
    dependency covers aggregation-free Parquet output. APPEND so each hospital
    adds files instead of clobbering existing partitions.
    """
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)
        # Pin types so every hospital's file agrees: a hospital with all-empty
        # cash would otherwise infer VARCHAR, and the glob union would promote the
        # column to VARCHAR (breaking median()). String cols VARCHAR, rest DOUBLE.
        str_cols = {"hospital_id", "state", "code", "code_type", "payer"}
        types = ", ".join(
            f"'{c}':'{'VARCHAR' if c in str_cols else 'DOUBLE'}'" for c in columns)
        con = duckdb.connect()
        con.execute(
            f"""COPY (SELECT {', '.join(columns)}
                      FROM read_csv('{tmp.replace(chr(92), '/')}', header=true, types={{{types}}}))
                TO '{out_dir.replace(chr(92), '/')}'
                (FORMAT PARQUET, PARTITION_BY (state, code), APPEND);"""
        )
        con.close()
    finally:
        os.remove(tmp)


def _write_report(dropped, report_dir, hospital_id):
    os.makedirs(report_dir, exist_ok=True)
    out = os.path.join(report_dir, f"dropped_{hospital_id}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "type_or_declared", "reason", "count"])
        for (code, t), d in sorted(dropped.items()):
            w.writerow([code, t, d["reason"], d["count"]])
    return out
