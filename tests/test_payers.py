"""Tests for the payer normalizer. pytest OR `python tests/test_payers.py`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from payers import normalize_payer  # noqa: E402


def test_brand_variants():
    assert normalize_payer("AETNA HEALTH INC") == "Aetna"
    assert normalize_payer("Aetna Better Health of Arizona") == "Aetna"
    assert normalize_payer("BCBS of Arizona") == "Blue Cross Blue Shield"
    assert normalize_payer("Blue Cross Blue Shield AZ") == "Blue Cross Blue Shield"
    assert normalize_payer("Anthem") == "Blue Cross Blue Shield"
    assert normalize_payer("CIGNA HealthCare") == "Cigna"
    assert normalize_payer("UnitedHealthcare") == "UnitedHealthcare"
    assert normalize_payer("UHC") == "UnitedHealthcare"
    assert normalize_payer("Optum Health") == "UnitedHealthcare"
    assert normalize_payer("Humana Gold Plus") == "Humana"


def test_brand_beats_medicare_medicaid():
    assert normalize_payer("Aetna Medicare Advantage") == "Aetna"          # brand wins
    assert normalize_payer("UnitedHealthcare Community Plan") == "UnitedHealthcare"


def test_government_and_unmatched():
    assert normalize_payer("AHCCCS") == "Medicaid"
    assert normalize_payer("Mercy Care") == "Medicaid"
    assert normalize_payer("Medicare") == "Medicare"
    assert normalize_payer("TRICARE West") == "Tricare"
    assert normalize_payer("Self-Pay") is None
    assert normalize_payer("") is None
    assert normalize_payer(None) is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
