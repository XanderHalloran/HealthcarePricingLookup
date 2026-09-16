"""Tests for the billing-code normalizer.

Runnable two ways:
    pytest tests/test_normalize.py
    python tests/test_normalize.py        (no framework needed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from normalize import normalize_code, normalize_lines, extract_amount, extract_amounts  # noqa: E402


def test_plain_cpt():
    r = normalize_code("73721")
    assert r.code == "73721" and r.code_type == "CPT" and r.matched


def test_cpt_with_description():
    r = normalize_code("73721 MRI LOWER EXTREMITY WITHOUT CONTRAST")
    assert r.code == "73721" and r.code_type == "CPT" and r.matched


def test_description_before_code():
    r = normalize_code("MRI LOWER EXTREMITY 73721")
    assert r.code == "73721" and r.code_type == "CPT" and r.matched


def test_cpt_category_ii_iii():
    assert normalize_code("3074F").code_type == "CPT"
    assert normalize_code("0184T").code_type == "CPT"


def test_hcpcs():
    r = normalize_code("j1234")
    assert r.code == "J1234" and r.code_type == "HCPCS" and r.matched


def test_hcpcs_with_modifier():
    r = normalize_code("J1234-RT")
    assert r.code == "J1234" and r.code_type == "HCPCS" and r.matched
    assert "modifier RT" in r.note


def test_revenue_code_is_flagged():
    r = normalize_code("0450")
    assert r.code_type == "REV" and r.matched is False
    assert "revenue code" in r.note


def test_drg_is_flagged_ambiguous():
    r = normalize_code("470")
    assert r.code_type == "DRG" and r.matched is False
    assert "assumed MS-DRG" in r.note


def test_tab_and_pipe_delimited():
    r = normalize_code("99213\tOffice visit | est patient")
    assert r.code == "99213" and r.code_type == "CPT"


def test_unknown():
    r = normalize_code("not a code at all")
    assert r.code is None and r.code_type == "UNKNOWN" and not r.matched


def test_empty_and_blank_lines_skipped():
    out = normalize_lines("73721\n\n   \nJ1234\n")
    assert [x.code for x in out] == ["73721", "J1234"]


def test_first_code_wins():
    # extra trailing columns must not change the resolved code
    r = normalize_code("73721, 100.00, 250.00, MRI")
    assert r.code == "73721"


def test_extract_amount_strong_signals():
    assert extract_amount("73721 MRI lower extremity $2,400.00", "73721") == 2400.0
    assert extract_amount("73721,MRI,2400.00,1200.00", "73721") == 2400.0  # largest = charge
    assert extract_amount("85025 20.00", "85025") == 20.0


def test_extract_amount_bare_int_fallback_and_none():
    assert extract_amount("99213 office visit 350", "99213") == 350.0
    assert extract_amount("73721", "73721") is None        # only the code, no charge


def test_extract_amounts_charge_and_allowed():
    assert extract_amounts("73721 MRI 2,400.00 900.00", "73721") == [2400.0, 900.0]  # charge, allowed
    assert extract_amounts("73721 MRI 2,400.00", "73721") == [2400.0]
    assert extract_amounts("73721", "73721") == []


def test_modifier_captured():
    r = normalize_code("99213-25 office visit 350")
    assert r.code == "99213" and r.modifier == "25"
    assert normalize_code("73721 MRI 2400").modifier == ""


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
