"""Find and repair facilities that carry ANOTHER facility's street address.

config-ortho/contact.yaml was built by matching CMS Care Compare records to the manifest.
Where a system runs several campuses, the match fell through to a sibling, so 19 facilities
across 9 addresses ended up with a different hospital's address and phone -- e.g.
SUMMIT-SHOW-LOW (Show Low, AZ) carries Banner Casa Grande's, 200 km away. On a page whose
whole promise is "call this number and send the patient here", that is the worst failure.

The per-facility COORDINATES are independently sourced and correct, so the fix is to reverse
geocode the trusted point back to a street address.

    python tools/fix_contacts.py --check      # list suspects, change nothing (use in CI)
    python tools/fix_contacts.py              # repair addresses in place

A shared address is only suspicious when the two facilities are far apart; two campuses in
one building legitimately share one. SUSPECT_KM is that threshold.
"""
import argparse
import collections
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
UA = "ortho-price-tool/1.0 (ortho.traqqit.com; facility contact repair)"
SUSPECT_KM = 1.0


def load(name):
    import yaml
    d = yaml.safe_load(open(os.path.join(CFG, name), encoding="utf-8")) or {}
    return d.get("hospitals", d)


def haversine(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def suspects(contact, geo):
    """-> [(address, [ids...], max_km)] for addresses shared by facilities that are not co-located."""
    by = collections.defaultdict(list)
    for k, v in contact.items():
        a = (v.get("address", "").strip().lower(), v.get("city", "").strip().lower())
        if a[0]:
            by[a].append(k)
    out = []
    for a, ids in by.items():
        if len(ids) < 2:
            continue
        pts = [geo[i] for i in ids if geo.get(i)]
        far = max((haversine(p, q) for p in pts for q in pts), default=0)
        if far > SUSPECT_KM:
            out.append((a, sorted(ids), far))
    return sorted(out, key=lambda t: -t[2])


def reverse(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 1})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    a = d.get("address") or {}
    road = a.get("road")
    if not road:
        return None
    num = a.get("house_number")
    city = a.get("city") or a.get("town") or a.get("village") or a.get("suburb") or ""
    return {"address": f"{num} {road}".strip() if num else road,
            "city": city, "zip": a.get("postcode", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    contact, geo = load("contact.yaml"), load("geo.yaml")
    names = {h["id"]: h["name"] for h in load("hospitals.yaml")}

    bad = suspects(contact, geo)
    ids = sorted({i for _, group, _ in bad for i in group})
    print(f"{len(bad)} shared addresses between facilities >{SUSPECT_KM} km apart "
          f"({len(ids)} facilities)")
    for (addr, city), group, km in bad:
        print(f"\n  {addr}, {city}   [{km:.1f} km apart]")
        for i in group:
            print(f"    {i:32} {names.get(i, '?')[:54]}")
    if args.check:
        return 1 if bad else 0

    path = os.path.join(CFG, "contact.yaml")
    raw = open(path, encoding="utf-8").read()

    def street(a):
        """'1625 North Campbell Avenue' -> 'campbell' : compare streets, not house numbers,
        because the two sources spell 'W Nine Mile Rd' and 'West 9 Mile Road' differently."""
        w = [x for x in re.sub(r"[^a-z0-9 ]", " ", (a or "").lower()).split()
             if x not in {"n", "s", "e", "w", "north", "south", "east", "west", "road", "rd",
                          "street", "st", "avenue", "ave", "boulevard", "blvd", "drive", "dr",
                          "parkway", "pkwy", "lane", "ln", "way", "circle", "highway", "hwy"}]
        return " ".join(w[1:] if w and w[0].isdigit() else w)

    got = {}
    for i in ids:
        pt = geo.get(i)
        if not pt:
            continue
        try:
            got[i] = reverse(pt[0], pt[1])
        except Exception as e:
            print(f"  ! {i}: {e}")
        time.sleep(1.1)

    # In each group ONE facility legitimately owns the shared address. Identify it by its own
    # reverse geocode agreeing with that address, and leave its (more complete, house-numbered)
    # record alone -- rewriting it replaced a correct address with a vaguer one.
    fixed = {}
    for (addr, _city), group, _km in bad:
        owner = next((i for i in group
                      if got.get(i) and street(got[i]["address"]) == street(addr)), None)
        for i in group:
            if i == owner or not got.get(i):
                continue
            new = got[i]
            if not re.match(r"^\d", new["address"]):
                print(f"  ! {i}: reverse geocode gave no house number ({new['address']!r}) "
                      f"-- left alone, needs a manual lookup")
                continue
            fixed[i] = new
            print(f"  {i:30} {contact[i].get('address','')!r} -> {new['address']!r}, "
                  f"{new['city']} {new['zip']}")
        if owner:
            print(f"  {owner:30} keeps {addr!r} (its own coordinate confirms it)")

    # rewrite the three scalar keys under each fixed id, leaving phone and comments alone
    out, cur, n = [], None, 0
    for line in raw.splitlines(True):
        m = re.match(r"^(\s+)([A-Z0-9][A-Z0-9-]*):\s*$", line)
        if m:
            cur = m.group(2)
        if cur in fixed:
            k = re.match(r"^(\s+)(address|city|zip):\s*(.*)$", line)
            if k:
                out.append(f'{k.group(1)}{k.group(2)}: "{fixed[cur][k.group(2)]}"\n')
                n += 1
                continue
        out.append(line)
    if n:
        open(path, "w", encoding="utf-8", newline="\n").write("".join(out))
    print(f"\nrewrote {n} fields across {len(fixed)} facilities")
    print("NOTE: phone numbers are NOT derivable from a coordinate. The facilities above that "
          "also shared a phone still need one looked up by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
