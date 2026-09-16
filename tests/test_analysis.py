"""Tests for bill-analysis helpers. pytest OR `python tests/test_analysis.py`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from analysis import detect_issues, build_letter, savings_summary  # noqa: E402


def _row(code, your_price=None, neg_high=None, neg_median=None, medicare=None, target=None,
         desc="", modifier="", allowed=None):
    return {"code": code, "code_type": "CPT", "resolved": True, "input": code, "modifier": modifier,
            "result": {"description": desc, "your_price": your_price, "neg_high": neg_high,
                       "neg_median": neg_median, "medicare_rate": medicare, "allowed": allowed,
                       "medicare_missing": medicare is None, "target_ask": target}}


def _titles(rows):
    return " | ".join(i["title"].lower() for i in detect_issues(rows))


def test_flags_duplicate_codes():
    assert "duplicate" in _titles([_row("80053"), _row("80053")])
    assert "duplicate" not in _titles([_row("80053"), _row("85027")])


def test_flags_charge_above_range():
    assert "above the local range" in _titles([_row("73721", your_price=5000, neg_high=1784)])
    assert "above the local range" not in _titles([_row("73721", your_price=1500, neg_high=1784)])


def test_flags_er_no_surprises_and_high_em():
    t = _titles([_row("99284", your_price=2000)])
    assert "no surprises act" in t and "visit codes" in t


def test_flags_unbundling():
    assert "unbundling" in _titles([_row("80053"), _row("80048")])      # CMP includes BMP
    assert "unbundling" in _titles([_row("45385"), _row("45378")])      # therapeutic bundles diagnostic
    assert "unbundling" in _titles([_row("74176"), _row("74177")])      # should be combined 74178
    assert "unbundling" not in _titles([_row("80053"), _row("85025")])  # unrelated codes


def test_flags_preventive_charged():
    assert "preventive" in _titles([_row("77067", your_price=250)])     # screening mammo charged
    assert "preventive" not in _titles([_row("77067", your_price=0)])   # $0 -> no flag
    assert "preventive" not in _titles([_row("73721", your_price=250)]) # not preventive


def test_flags_scrutinized_modifiers():
    assert "modifier 25" in _titles([_row("99213", modifier="25")])
    assert "modifier 59" in _titles([_row("45385", modifier="59")])
    assert "modifier" not in _titles([_row("99213", modifier="")])


def test_flags_balance_bill():
    t = _titles([_row("73721", your_price=2400, allowed=900)])
    assert "balance bill" in t
    assert "balance bill" not in _titles([_row("73721", your_price=900, allowed=900)])
    assert "balance bill" not in _titles([_row("73721", your_price=2400)])   # no allowed given


def test_gfe_dispute_flag():
    rows = [_row("73721", your_price=2000), _row("99284", your_price=1500)]   # total 3500
    over = " | ".join(i["title"].lower() for i in detect_issues(rows, gfe=1000))
    assert "exceeds your good-faith-estimate" in over
    within = " | ".join(i["title"].lower() for i in detect_issues(rows, gfe=3300))
    assert "good-faith-estimate" not in within     # within $400 -> no flag


def test_letter_includes_gfe_paragraph():
    rows = [_row("73721", your_price=2000, target=500, medicare=300, neg_median=1000, desc="MRI")]
    text = build_letter(rows, "AZ", gfe=500)       # 2000 >= 500 + 400
    assert "Good-Faith-Estimate of $500" in text and "Patient-Provider Dispute" in text


def test_build_letter_has_numbers_and_citations():
    text = build_letter([_row("73721", your_price=3200, neg_median=1070, medicare=241,
                              target=483, desc="MRI lower extremity")], "AZ")
    for s in ["73721", "$3,200", "$241", "$1,070", "$483", "45 CFR 180", "501(r)"]:
        assert s in text, f"missing {s}"


def test_savings_summary():
    # target used when present; capped at charged so an above-charge target never inflates savings
    rows = [_row("73721", your_price=2400, target=500, neg_median=1000),
            _row("80053", your_price=450, neg_median=600)]   # median>charge -> capped, 0 saved here
    s = savings_summary(rows)
    assert s["n"] == 2 and s["charged"] == 2850
    assert s["target"] == 950            # 500 + min(600,450)=450
    assert s["savings"] == 1900          # 2850 - 950
    assert savings_summary([_row("73721")]) is None      # no charge entered -> no summary


def test_assistant_no_key_degrades():
    import assistant
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert assistant.ask("anything", "ctx") == ""    # no key -> empty, endpoint shows notice
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
    ctx = assistant.build_context(
        [_row("73721", your_price=2400, target=500, neg_median=1000, medicare=241, desc="MRI")], [], "AZ")
    assert "73721" in ctx and "$2,400" in ctx and "$500" in ctx


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
