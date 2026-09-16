"""Soft email capture. pytest OR `python tests/test_capture.py`. No network, no SMTP."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ["CAPTURE_DB"] = os.path.join(tempfile.mkdtemp(), "cap.sqlite")
os.environ.pop("SMTP_HOST", None)

import capture  # noqa: E402


def test_email_validation():
    assert capture.valid_email("  Some.One@Example.org ") == "some.one@example.org"
    for bad in ("", "nope", "a@b", "a b@c.com", "@x.com", "x@y.c"):
        assert capture.valid_email(bad) is None, bad


def test_rate_limit_sliding_window():
    capture._hits.clear()
    t = 1_000_000.0
    assert all(capture.allow("1.2.3.4", t + i) for i in range(capture.RATE_PER_HOUR))
    assert not capture.allow("1.2.3.4", t + 10)                 # 6th within the hour -> blocked
    assert capture.allow("5.6.7.8", t + 10)                     # other IP unaffected
    assert capture.allow("1.2.3.4", t + 3601)                   # window slid


def test_send_without_smtp_is_false_and_record_persists():
    assert not capture.configured()
    assert capture.send("a@b.co", "s", "b") is False
    rid = capture.record("a@b.co", "letter", "AZ", "phoenix", "73721", "", False, "1.1.1.1", False)
    assert rid >= 1
    import sqlite3
    with sqlite3.connect(os.environ["CAPTURE_DB"]) as con:
        assert con.execute("select email, kind, sent from captures where id=?", (rid,)).fetchone() == ("a@b.co", "letter", 0)


def test_compare_summary_lists_hospitals_and_disclaimer():
    rows = [{"resolved": True, "code": "73721", "result": {
        "description": "MRI knee", "medicare_rate": 250.0, "neg_median": 600.0, "target_ask": 500.0,
        "hospitals": [{"name": "Cheap Hosp", "price": 400.0, "outlier": None},
                      {"name": "Pricey Hosp", "price": 9000.0, "outlier": "high"}]}},
            {"resolved": False, "code": None, "result": None}]
    txt = capture.compare_summary(rows, "Arizona", "Phoenix Metro", "https://x/?state=AZ")
    assert "73721" in txt and "$250" in txt and "$600" in txt and "$500" in txt
    assert "Cheap Hosp" in txt and "gross charge" in txt and "not a guarantee" in txt and "https://x/?state=AZ" in txt


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
