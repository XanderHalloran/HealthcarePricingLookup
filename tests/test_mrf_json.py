"""Tests for the JSON MRF parser + JSON ingest. pytest OR `python tests/test_mrf_json.py`."""
import codecs
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
FIX = os.path.join(HERE, "fixtures")
ALLOW = os.path.join(FIX, "allowlist_test.yaml")

import duckdb  # noqa: E402
from mrf_json import parse_mrf_json  # noqa: E402
from ingest import ingest_mrf, rates_source  # noqa: E402


def test_parse_json_yields_one_row_per_payer():
    rows = list(parse_mrf_json(os.path.join(FIX, "mrf_sample.json")))
    mri = [r for r in rows if r.codes and r.codes[0][0] == "73721"]
    assert len(mri) == 2                                   # one row per payer
    assert {r.negotiated_dollar for r in mri} == {800, 1200}
    assert all(r.cash == 950 for r in mri)
    assert mri[0].codes == [("73721", "CPT"), ("0610", "RC")]


def test_json_ingest_aggregates_and_filters():
    with tempfile.TemporaryDirectory() as tmp:
        out, rep = os.path.join(tmp, "hr"), os.path.join(tmp, "rep")
        s = ingest_mrf(os.path.join(FIX, "mrf_sample.json"), "JSONH", "AZ", ALLOW, out, rep)
        assert s["codes_kept"] == 1                        # 73721 (85025 not in test allowlist, 99999 dropped)
        con = duckdb.connect()
        row = con.execute(
            f"SELECT cash_price, negotiated_min, negotiated_median, negotiated_max "
            f"FROM {rates_source(out)} WHERE code='73721'").fetchone()
        assert row == (950, 800, 1000, 1200)               # median of [800,1200] = 1000


def test_bom_prefixed_json_parses():
    import tempfile
    src = open(os.path.join(FIX, "mrf_sample.json"), "rb").read()
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "wb") as f:
        f.write(codecs.BOM_UTF8 + src)
    try:
        assert len(list(parse_mrf_json(tmp))) == len(list(parse_mrf_json(os.path.join(FIX, "mrf_sample.json"))))
    finally:
        os.remove(tmp)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
