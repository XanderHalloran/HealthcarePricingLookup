"""Sharpen the low-precision coordinates in config-ortho/geo.yaml.

58 of 301 entries are stored at 2 or fewer decimal places (+-1.1 km or worse), which was
invisible when the map framed a 25-mile radius ring and is very visible now that it frames
the results and reaches street zoom: the pin sits a block or two off, sometimes the far side
of a freeway, next to a phone number that is correct.

config-ortho/contact.yaml already holds a real street address for all 301, so this is a
batch re-geocode, not research.

    python tools/regeocode.py --dry-run     # report only
    python tools/regeocode.py               # rewrite geo.yaml

ponytail: refuses any result more than MAX_SHIFT_KM from the stored point, so a bad
Nominatim match can only be dropped, never silently relocate a hospital.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config-ortho")
UA = "ortho-price-tool/1.0 (ortho.traqqit.com; facility geocoding)"
MAX_SHIFT_KM = 15.0          # a refinement, never a relocation
PRECISE = 3                  # decimals we consider good enough (~110 m)


def decimals(v):
    m = re.match(r"^-?\d+(?:\.(\d+))?$", str(v))
    return len(m.group(1)) if m and m.group(1) else 0


def haversine(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def geocode(q):
    """Nominatim forward search. Searching by FACILITY NAME hits the hospital POI directly and
    proved exact on every one of six hand-checked cases; searching by street address is the
    fallback, because a few manifest names do not exist in OSM."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "countrycodes": "us"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        hits = json.load(r)
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import yaml

    geo_path = os.path.join(CFG, "geo.yaml")
    raw = open(geo_path, encoding="utf-8").read()
    geo = (yaml.safe_load(raw) or {})
    geo = geo.get("hospitals", geo)
    contact = (yaml.safe_load(open(os.path.join(CFG, "contact.yaml"), encoding="utf-8")) or {})
    contact = contact.get("hospitals", contact)
    hs = yaml.safe_load(open(os.path.join(CFG, "hospitals.yaml"), encoding="utf-8"))["hospitals"]
    names = {h["id"]: h["name"].split(" (")[0] for h in hs}

    fuzzy = [k for k, v in geo.items()
             if v and min(decimals(v[0]), decimals(v[1])) < PRECISE]
    print(f"{len(geo)} facilities, {len(fuzzy)} below {PRECISE} decimals")
    if args.dry_run:
        for k in fuzzy:
            c = contact.get(k) or {}
            print(f"  {k:32} {geo[k]}  {c.get('address', '(no address)')}, {c.get('city', '')}")
        return

    fixed, skipped = {}, []
    for i, k in enumerate(fuzzy, 1):
        c = contact.get(k) or {}
        if not c.get("address"):
            skipped.append((k, "no address"))
            continue
        queries = [f"{names.get(k, '')}, {c.get('city', '')}".strip(", "),
                   f"{c['address']}, {c.get('city', '')} {c.get('zip', '')}".strip()]
        try:
            hit = None
            for q in queries:
                hit = geocode(q)
                time.sleep(1.1)                           # Nominatim: 1 request/second
                if hit and haversine(geo[k], hit) <= MAX_SHIFT_KM:
                    break
                hit = None
        except Exception as e:
            skipped.append((k, f"geocoder: {e}"))
            continue
        if not hit:
            skipped.append((k, "no match"))
            continue
        d = haversine(geo[k], hit)
        fixed[k] = [round(hit[0], 5), round(hit[1], 5)]
        print(f"  [{i}/{len(fuzzy)}] {k:30} {geo[k]} -> {fixed[k]}  ({d*1000:.0f} m)")

    # rewrite in place, line by line, so comments and ordering survive
    out, n = [], 0
    for line in raw.splitlines(True):
        m = re.match(r"^(\s+)([A-Z0-9][A-Z0-9-]*):\s*\[", line)
        if m and m.group(2) in fixed:
            la, lo = fixed[m.group(2)]
            out.append(f"{m.group(1)}{m.group(2)}: [{la}, {lo}]\n")
            n += 1
        else:
            out.append(line)
    if n:
        open(geo_path, "w", encoding="utf-8", newline="\n").write("".join(out))
    print(f"\nrewrote {n} coordinates; {len(skipped)} left alone")
    for k, why in skipped:
        print(f"  skipped {k}: {why}")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
