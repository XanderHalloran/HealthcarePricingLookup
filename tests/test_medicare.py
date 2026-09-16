"""Tests for the Medicare reference loaders. pytest OR `python tests/test_medicare.py`."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
FIX = os.path.join(HERE, "fixtures")
SCHEMA = os.path.join(HERE, "..", "src", "schema.sql")

from medicare import build_reference_db, load_pfs, load_opps, load_clfs  # noqa: E402

CF = 32.74


def _db():
    tmp = tempfile.mkdtemp()
    return build_reference_db(os.path.join(tmp, "ref.duckdb"), SCHEMA)


def test_pfs_rate_is_nonfacility_total_times_cf():
    con = _db()
    n = load_pfs(con, os.path.join(FIX, "pfs_sample.csv"), 2024, CF)
    assert n == 2                                   # 73721 + 99213, two-row CMS header parsed
    rate = con.execute(
        "SELECT rate FROM medicare_rates WHERE code='73721' AND source='PFS'").fetchone()[0]
    assert rate == round(6.19 * CF, 2)              # non-facility total RVU * conversion factor
    office = con.execute(
        "SELECT rate FROM medicare_rates WHERE code='99213' AND source='PFS'").fetchone()[0]
    assert office == round(2.40 * CF, 2)


def test_opps_rate_direct_and_type_inference():
    con = _db()
    load_opps(con, os.path.join(FIX, "opps_sample.csv"), 2024)
    rows = dict(con.execute(
        "SELECT code, code_type FROM medicare_rates WHERE source='OPPS'").fetchall())
    assert rows["73721"] == "CPT" and rows["G0463"] == "HCPCS"
    rate = con.execute(
        "SELECT rate FROM medicare_rates WHERE code='73721' AND source='OPPS'").fetchone()[0]
    assert rate == 313.40


def test_clfs_rate_for_labs():
    con = _db()
    n = load_clfs(con, os.path.join(FIX, "clfs_sample.csv"), 2025)
    assert n >= 2                                   # dedup of base + QW handled by INSERT OR REPLACE
    cmp_ = con.execute(
        "SELECT rate, code_type FROM medicare_rates WHERE code='80053' AND source='CLFS'").fetchone()
    assert cmp_ == (10.56, "CPT")
    cbc = con.execute(
        "SELECT rate FROM medicare_rates WHERE code='85027' AND source='CLFS'").fetchone()[0]
    assert cbc == 7.77


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
