"""End-to-end ingest tests against fixtures. pytest OR `python tests/test_ingest.py`."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
FIX = os.path.join(HERE, "fixtures")
ALLOW = os.path.join(FIX, "allowlist_test.yaml")

import duckdb  # noqa: E402
from ingest import ingest_mrf, rates_source  # noqa: E402


def _query(out_dir):
    con = duckdb.connect()
    rows = con.execute(
        "SELECT code, code_type, cash_price, negotiated_min, negotiated_median, "
        f"negotiated_max, state FROM {rates_source(out_dir)}"
    ).fetchall()
    con.close()
    return {r[0]: r for r in rows}


def test_clean_ingest_aggregates_and_filters():
    with tempfile.TemporaryDirectory() as tmp:
        out, rep = os.path.join(tmp, "hr"), os.path.join(tmp, "rep")
        summary = ingest_mrf(os.path.join(FIX, "mrf_clean.csv"), "H1", "ma", ALLOW, out, rep)

        assert summary["codes_kept"] == 2                  # 73721, 85025 (12345 dropped)
        by_code = _query(out)
        mri = by_code["73721"]
        assert mri[1] == "CPT" and mri[2] == 1000          # cash
        assert mri[3] == 900 and mri[4] == 1100 and mri[5] == 1300   # min/median/max
        assert mri[6] == "MA"                              # state partition, uppercased
        assert by_code["85025"][4] == 18                   # single payer -> median == that value
        assert "12345" not in by_code


def test_clean_drop_report_lists_unlisted_code():
    with tempfile.TemporaryDirectory() as tmp:
        out, rep = os.path.join(tmp, "hr"), os.path.join(tmp, "rep")
        ingest_mrf(os.path.join(FIX, "mrf_clean.csv"), "H1", "MA", ALLOW, out, rep)
        report = os.path.join(rep, "dropped_H1.csv")
        text = open(report, encoding="utf-8").read()
        assert "12345" in text and "not_in_allowlist" in text


def test_deviant_keeps_drg_and_hcpcs_drops_revenue_and_unparseable():
    with tempfile.TemporaryDirectory() as tmp:
        out, rep = os.path.join(tmp, "hr"), os.path.join(tmp, "rep")
        summary = ingest_mrf(os.path.join(FIX, "mrf_deviant.csv"), "H2", "TX", ALLOW, out, rep)

        by_code = _query(out)
        assert set(by_code) == {"G0463", "470"}            # both allowlisted & kept
        assert by_code["470"][1] == "DRG"                  # declared MS-DRG accepted despite heuristic
        assert summary["codes_kept"] == 2

        text = open(os.path.join(rep, "dropped_H2.csv"), encoding="utf-8").read()
        assert "0450" in text                              # revenue code dropped (unsupported type)
        assert "ABCDE" in text                             # unparseable dropped


def test_payer_rates_written():
    with tempfile.TemporaryDirectory() as tmp:
        out, rep, pay = os.path.join(tmp, "hr"), os.path.join(tmp, "rep"), os.path.join(tmp, "pay")
        ingest_mrf(os.path.join(FIX, "mrf_clean.csv"), "H1", "MA", ALLOW, out, rep, pay)
        con = duckdb.connect()
        rows = dict(con.execute(
            f"SELECT payer, negotiated_rate FROM {rates_source(pay)} WHERE code='73721'").fetchall())
        assert rows.get("Aetna") == 900
        assert rows.get("Blue Cross Blue Shield") == 1100 and rows.get("Cigna") == 1300


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
