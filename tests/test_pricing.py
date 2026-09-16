"""Tests for pricing lookup + coaching. pytest OR `python tests/test_pricing.py`."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
FIX = os.path.join(HERE, "fixtures")
CFG = os.path.join(HERE, "..", "config")
SCHEMA = os.path.join(HERE, "..", "src", "schema.sql")
ALLOW = os.path.join(FIX, "allowlist_test.yaml")
TEMPLATES = os.path.join(CFG, "templates.yaml")

from medicare import build_reference_db, load_pfs, load_opps  # noqa: E402
from ingest import ingest_mrf  # noqa: E402
from pricing import (  # noqa: E402
    lookup, target_ask, render_points, analyze, load_templates, load_services,
    get_hospital_breakdown, price_stats, haversine,
)

CF = 32.74


def _setup(tmp):
    con = build_reference_db(":memory:", SCHEMA)
    load_pfs(con, os.path.join(FIX, "pfs_sample.csv"), 2024, CF)
    load_opps(con, os.path.join(FIX, "opps_sample.csv"), 2024)
    rates, rep, pay = (os.path.join(tmp, d) for d in ("hr", "rep", "pay"))
    ingest_mrf(os.path.join(FIX, "mrf_clean.csv"), "H1", "MA", ALLOW, rates, rep, pay)
    ingest_mrf(os.path.join(FIX, "mrf_deviant.csv"), "H2", "TX", ALLOW, rates, rep, pay)
    return con, rates, pay


def test_target_ask_picks_lower():
    assert target_ask(313.40, 1000, 2.0) == (626.80, "medicare")   # 2x medicare < cash
    assert target_ask(11.05, 20, 2.0) == (20, "cash")              # cash < 2x medicare
    assert target_ask(None, None, 2.0) == (None, None)


def test_lookup_uses_opps_for_outpatient_and_local_cash():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, pay = _setup(tmp)
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0)
        assert r["medicare_source"] == "OPPS" and r["medicare_rate"] == 313.40
        # Range is now the robust distribution over per-facility prices; MA has one
        # hospital (price = its $1,000 cash), so band + min/max collapse to 1000.
        assert r["cash_price"] == 1000 and r["neg_median"] == 1000
        assert r["neg_low"] == 1000 and r["neg_high"] == 1000
        assert r["neg_min"] == 1000 and r["neg_max"] == 1000
        assert r["target_ask"] == 626.80 and r["target_basis"] == "medicare"
        assert not r["medicare_missing"] and not r["hospital_missing"]


def test_render_fills_numbers_and_always_on_points():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, pay = _setup(tmp)
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI lower extremity", 2.0)
        pts = {p["id"]: p for p in render_points(r, load_templates(TEMPLATES))}
        assert "$313.40" in pts["vs_medicare"]["text"]
        assert "$626.80" in pts["vs_medicare"]["text"]
        assert "$1,000.00" in pts["vs_local_cash"]["text"]
        assert pts["request_itemized"]["text"] and pts["financial_assistance"]["text"]
        assert not pts["vs_medicare"]["missing"]


def test_missing_data_emits_notes_not_silence():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, pay = _setup(tmp)
        r = lookup(con, rates, "99999", "CPT", "MA", "", 2.0)   # in neither dataset
        assert r["medicare_missing"] and r["hospital_missing"]
        pts = {p["id"]: p for p in render_points(r, load_templates(TEMPLATES))}
        assert pts["vs_medicare"]["missing"] and "No Medicare benchmark" in pts["vs_medicare"]["text"]
        assert pts["vs_local_cash"]["missing"] and "No local cash price" in pts["vs_local_cash"]["text"]
        assert "request_itemized" in pts and "financial_assistance" in pts


def test_analyze_block_resolves_and_flags():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, pay = _setup(tmp)
        templates = load_templates(TEMPLATES)
        descs = {(s["code"], s["type"]): s["desc"] for s in load_services(ALLOW)}
        rows = analyze(con, rates, "73721 MRI lower extremity\nnot a real line\n85025",
                       "MA", templates, descs, 2.0)
        by_in = {r["code"]: r for r in rows}
        assert by_in["73721"]["resolved"] and by_in["73721"]["result"]["target_ask"] == 626.80
        assert by_in["85025"]["resolved"] and by_in["85025"]["result"]["target_basis"] == "cash"
        unresolved = [r for r in rows if not r["resolved"]]
        assert len(unresolved) == 1 and unresolved[0]["input"] == "not a real line"


def test_hospital_breakdown_lists_per_hospital_cheapest_first():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, pay = _setup(tmp)
        bd = get_hospital_breakdown(con, rates, "73721", "MA")
        assert [h["hospital_id"] for h in bd] == ["H1"]
        assert bd[0]["price"] == 1000          # cash/self-pay price
        # surfaced in the lookup result for the UI, with quality stars
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0,
                   {"H1": "General Hospital"}, {"H1": 4})
        assert r["hospitals"][0]["name"] == "General Hospital"
        assert r["hospitals"][0]["stars"] == 4


def test_payer_aware_breakdown_and_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, pay = _setup(tmp)
        # per-hospital breakdown switches to the chosen insurer's negotiated rate
        bd = get_hospital_breakdown(con, rates, "73721", "MA", payer="Cigna", payer_dir=pay)
        assert [(b["hospital_id"], b["price"], b["rate_type"]) for b in bd] == [("H1", 1300, "Cigna")]
        # lookup retitles the range/median to that payer
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, payer_dir=pay, payer="Cigna")
        assert r["payer"] == "Cigna" and r["neg_median"] == 1300
        assert r["hospitals"][0]["rate_type"] == "Cigna"
        # default (no payer) still uses cash/median
        assert lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, payer_dir=pay)["payer"] is None


def test_freestanding_merged_cheapest_first():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, _ = _setup(tmp)
        fs = {"73721": [{"name": "SimonMed", "type": "imaging", "city": "Phoenix", "price": 200.0, "state": "MA"}]}
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0,
                   {"H1": "General Hospital"}, freestanding=fs)
        top = r["hospitals"][0]                       # H1 cash is 1000; freestanding 200 wins
        assert top["name"] == "SimonMed" and top["type"] == "imaging" and top["price"] == 200.0
        assert any(h["type"] == "hospital" for h in r["hospitals"])   # hospital still listed


def test_metro_filter_restricts_range_and_freestanding():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, _ = _setup(tmp)
        names = {"H1": "General Hospital"}
        # metro that INCLUDES H1 -> keeps it, range recomputed from its price (cash 1000)
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, names,
                   metro="m_in", metro_ids={"H1"})
        assert [h["name"] for h in r["hospitals"] if h["type"] == "hospital"] == ["General Hospital"]
        assert r["neg_low"] == 1000 and r["neg_high"] == 1000 and r["n_hospitals"] == 1
        # metro that EXCLUDES H1 -> empty facility list + null range (no cross-metro leakage)
        r2 = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, names,
                    metro="m_out", metro_ids=set())
        assert not [h for h in r2["hospitals"] if h["type"] == "hospital"]
        assert r2["neg_median"] is None and r2["n_hospitals"] == 0
        # a Phoenix freestanding center shows in phoenix but NOT in a tucson view
        fs = {"73721": [{"name": "SimonMed", "type": "imaging", "city": "Phoenix",
                         "price": 200.0, "metro": "phoenix", "state": "MA"}]}
        rt = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, names,
                    freestanding=fs, metro="tucson", metro_ids={"H1"})
        assert not any(h["name"] == "SimonMed" for h in rt["hospitals"])
        rp = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, names,
                    freestanding=fs, metro="phoenix", metro_ids={"H1"})
        assert any(h["name"] == "SimonMed" for h in rp["hospitals"])
        # no metro passed -> statewide (H1 present; band = its price 1000)
        rn = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, names)
        assert rn["neg_low"] == 1000 and any(h["type"] == "hospital" for h in rn["hospitals"])


def test_price_stats_excludes_outliers():
    # $20 is implausible for a Medicare-$300 service (floor 0.25x = $75) -> excluded
    st = price_stats([900, 1000, 1050, 1100, 1200, 20], medicare=300)
    assert st["n"] == 6 and st["n_used"] == 5 and st["n_excluded"] == 1
    assert st["flag"](20) == "low" and st["flag"](1000) is None
    assert st["min"] == 20 and st["max"] == 1200
    assert st["median"] == 1050          # median of the kept 5, not pulled by the $20
    # a wildly high price is flagged 'high' and dropped from the median (needs enough
    # clean points that the outlier doesn't drag Q3 up and mask itself)
    hi = price_stats([900, 950, 1000, 1020, 1050, 1080, 1100, 1150, 9000], medicare=300)
    assert hi["flag"](9000) == "high" and hi["n_excluded"] == 1
    # small samples: no statistical fences, but the hard floor still applies
    assert price_stats([500], medicare=300)["n_used"] == 1
    assert price_stats([]) is None


def test_haversine_miles():
    phx, tuc = (33.4484, -112.0740), (32.2226, -110.9747)
    d = haversine(phx, tuc)
    assert 100 < d < 115            # Phoenix–Tucson ~107 mi
    assert haversine(None, phx) is None


def test_lookup_distance_and_outlier_flag():
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, _ = _setup(tmp)
        r = lookup(con, rates, "73721", "CPT", "MA", "MRI", 2.0, {"H1": "Gen"},
                   user_loc=(33.0, -112.0), geo={"H1": (32.22, -110.97)})
        h = next(x for x in r["hospitals"] if x["type"] == "hospital")
        assert h["distance"] and h["distance"] > 0        # miles from user to H1
        assert h.get("outlier") is None and h.get("cheapest")   # single hospital: not an outlier, is cheapest
        assert r["neg_min"] == 1000 and r["neg_max"] == 1000


def test_freestanding_never_leaks_across_states():
    """Multi-state: an AZ imaging center must not appear in another state's list."""
    with tempfile.TemporaryDirectory() as tmp:
        con, rates, _ = _setup(tmp)
        fs = {"73721": [{"name": "AZ Imaging", "type": "imaging", "city": "Phoenix",
                         "price": 200.0, "metro": "phoenix", "state": "AZ"}]}
        az = lookup(con, rates, "73721", "CPT", "AZ", freestanding=fs)
        ma = lookup(con, rates, "73721", "CPT", "MA", freestanding=fs)
        assert any(h["name"] == "AZ Imaging" for h in az["hospitals"])
        assert not any(h["name"] == "AZ Imaging" for h in ma["hospitals"])


def test_load_metros_carries_state_registry():
    """Multi-state: every metro maps to a state, states list + promote flags load."""
    import os
    from pricing import load_metros
    m = load_metros(os.path.join(HERE, "..", "config", "metros.yaml"))
    codes = {s["code"] for s in m["states"]}
    assert "AZ" in codes and all(len(c) == 2 for c in codes)
    assert set(m["state_of"].values()) <= codes            # no metro on an unknown state
    assert all(k in m["state_of"] for k in m["ids"])
    assert any(x["promote"] for x in m["list"])              # sitemap has something to promote
    slugs = [s["slug"] for s in m["states"]]
    assert len(slugs) == len(set(slugs)) and not (set(slugs) & set(m["ids"]))  # slugs vs metro keys


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
