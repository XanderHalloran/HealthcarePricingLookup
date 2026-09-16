"""Apply a state research report to the config files.

    python tools/add_state.py <report.md> <STATE_CODE> "<State Name>" <slug> [promote-metro-key ...]
    python tools/add_state.py --addendum <report.md> <STATE_CODE>     # merge a "# ADDENDUM" section

Reads the ```yaml block (hospitals/metros/geo/ratings) from the report, unescapes HTML
entities the agent may have left in, and APPENDS to config/hospitals.yaml, metros.yaml,
geo.yaml, quality.yaml + registers the state in metros.yaml `states`. Addendum mode
expects the state to exist already and accepts metros as `{key: [ids]}` additions.
ponytail: text append, not a YAML rewrite, so the hand-written comments/ordering survive.
"""
import datetime
import html
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _append(p, text):
    _write(p, _read(p).rstrip("\n") + "\n" + text)


def _add_ids_to_metro(text, key, new_ids):
    """Insert ids at the end of an existing metro's `ids:` list (text-level, comments kept)."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == f"- key: {key}")
    i = next(i for i in range(start, len(lines)) if lines[i].strip() == "ids:") + 1
    while i < len(lines) and lines[i].startswith("      - "):
        i += 1
    lines[i:i] = [f"      - {x}" for x in new_ids]
    return "\n".join(lines)


def _hospital_line(h, code):
    parts = [f"id: {h['id']}", f'name: "{h["name"]}"', f"state: {code}"]
    if h.get("ccn"):
        parts.append(f'ccn: "{h["ccn"]}"')
    if h.get("format") == "json":
        parts.append("format: json")
    if h.get("unzip"):
        parts.append("unzip: true")
    parts.append(f'url: "{h["url"]}"')
    return "  - { " + ", ".join(parts) + " }"


def main(report, code, name=None, slug=None, promote=(), addendum=False):
    code = code.upper()
    body = html.unescape(_read(report))
    if addendum:
        body = body.split("# ADDENDUM", 1)[1]
    m = re.search(r"```yaml\s*(.*?)```", body, re.S)
    data = yaml.safe_load(m.group(1))
    hosp, metros = data["hospitals"], data.get("metros") or []
    geo, ratings = data.get("geo") or {}, data.get("ratings") or {}
    if isinstance(metros, dict):                       # addendum shape: {key: [ids]}
        metros = [{"key": k, "ids": v} for k, v in metros.items()]

    existing = yaml.safe_load(_read(os.path.join(CFG, "metros.yaml")))
    registered = {s["code"] for s in existing["states"]}
    assert (code in registered) == addendum, f"{code} registered={code in registered}"
    known_ids = {h["id"] for h in yaml.safe_load(_read(os.path.join(CFG, "hospitals.yaml")))["hospitals"]}
    known_keys = {m_["key"] for m_ in existing["metros"]} | {s["slug"] for s in existing["states"]}
    ids = [h["id"] for h in hosp]
    assert len(ids) == len(set(ids)) and not (set(ids) & known_ids), "duplicate hospital id"
    for h in hosp:
        assert str(h["state"]).upper() == code, h
    for m_ in metros:
        assert set(m_["ids"]) <= set(ids), f"metro {m_['key']} references unknown id"
        if m_["key"] in known_keys:
            assert addendum and m_.get("label") is None, f"metro key {m_['key']} clashes"
    assert set(geo) <= set(ids) and set(ratings) <= set(ids)
    name = name or next(x["name"] for x in existing["states"] if x["code"] == code)

    tag = "addendum" if addendum else "added"
    _append(os.path.join(CFG, "hospitals.yaml"),
            f"\n  # ===== {name.upper()} ({code}) — {tag} {datetime.date.today()} =====\n"
            + "\n".join(_hospital_line(h, code) for h in hosp) + "\n")

    # metros.yaml — state registry line + block-style metros (or ids merged into existing ones)
    p = os.path.join(CFG, "metros.yaml")
    s = _read(p)
    if not addendum:
        s = s.replace("\nmetros:", f"  - {{ code: {code}, name: {name}, slug: {slug} }}\n\nmetros:", 1)
    for m_ in metros:
        if m_["key"] in known_keys:
            s = _add_ids_to_metro(s, m_["key"], m_["ids"])
    _write(p, s)
    blocks = []
    for m_ in (x for x in metros if x["key"] not in known_keys):
        b = [f"  - key: {m_['key']}", f"    state: {code}"]
        if m_["key"] in promote:
            b.append("    promote: true")
        b += [f'    label: "{m_["label"]}"', f'    slug_city: "{m_["slug_city"]}"', "    ids:"]
        b += [f"      - {i}" for i in m_["ids"]]
        blocks.append("\n".join(b))
    if blocks:
        _append(p, f"\n  # ===== {name} =====\n" + "\n\n".join(blocks) + "\n")

    if geo:
        _append(os.path.join(CFG, "geo.yaml"), f"  # {name} ({tag})\n" + "".join(
            f"  {i}: [{lat}, {lon}]\n" for i, (lat, lon) in geo.items()))
    if ratings:
        _append(os.path.join(CFG, "quality.yaml"), f"  # {name} ({tag})\n" + "".join(
            f"  {i}: {r}\n" for i, r in ratings.items()))
    print(f"{code} {tag}: {len(hosp)} hospitals, {len(metros)} metros, {len(geo)} geo, {len(ratings)} ratings")


if __name__ == "__main__":
    if sys.argv[1] == "--addendum":
        main(sys.argv[2], sys.argv[3], addendum=True)
    else:
        main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
