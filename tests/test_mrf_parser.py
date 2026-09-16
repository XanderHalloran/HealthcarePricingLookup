"""Tests for the defensive MRF parser. pytest OR `python tests/test_mrf_parser.py`."""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
FIX = os.path.join(HERE, "fixtures")

from mrf_parser import parse_mrf  # noqa: E402


def test_clean_rows_and_payers():
    rows = list(parse_mrf(os.path.join(FIX, "mrf_clean.csv")))
    mri = [r for r in rows if r.codes and r.codes[0][0] == "73721"]
    assert len(mri) == 3                                   # one row per payer
    assert {r.negotiated_dollar for r in mri} == {900, 1100, 1300}
    assert all(r.cash == 1000 for r in mri)
    assert mri[0].codes[0] == ("73721", "CPT")


def test_clean_includes_unlisted_code():
    rows = list(parse_mrf(os.path.join(FIX, "mrf_clean.csv")))
    assert any(r.codes and r.codes[0][0] == "12345" for r in rows)


def test_wide_layout_emits_one_row_per_payer():
    """Wide CSV (one negotiated_dollar column per payer, no payer_name) -> same shape as tall."""
    rows = list(parse_mrf(os.path.join(FIX, "mrf_wide.csv")))
    mri = [r for r in rows if r.codes[0][0] == "73721"]
    assert sorted((r.payer, r.negotiated_dollar) for r in mri) == [("aetna connected", 900.0), ("bcbs ppo", 1100.0)]
    assert all(r.cash == 1000.0 and r.charge_min == 900.0 and r.charge_max == 1300.0 for r in mri)
    cbc = [r for r in rows if r.codes[0][0] == "85025"]
    assert [(r.payer, r.negotiated_dollar) for r in cbc] == [("bcbs ppo", 18.0)]   # empty Aetna cell skipped
    cash_only = [r for r in rows if r.codes[0][0] == "12345"]
    assert len(cash_only) == 1 and cash_only[0].negotiated_dollar is None and cash_only[0].cash == 200.0


def test_deviant_spaced_headers():
    rows = list(parse_mrf(os.path.join(FIX, "mrf_deviant.csv")))
    g = [r for r in rows if r.codes and r.codes[0][0] == "G0463"][0]
    assert g.codes[0] == ("G0463", "HCPCS")
    assert g.cash == 150 and g.negotiated_dollar == 120 and g.charge_min == 120


def test_deviant_mixed_code_types_all_parsed():
    rows = list(parse_mrf(os.path.join(FIX, "mrf_deviant.csv")))
    seen = {r.codes[0][0]: r.codes[0][1] for r in rows if r.codes}
    assert seen["470"] == "MS-DRG"
    assert seen["0450"] == "RC"
    assert "ABCDE" in seen


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
