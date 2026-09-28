"""Apply a state research report to config-ortho/ (not config/).

    python tools/apply_ortho_state.py <report.md> <CODE> "<Name>" <slug> [hip_knee.yaml] [contact.yaml]

tools/add_state.py writes to config/, which is the CONSUMER site, so this points it at
config-ortho. It also works around the one thing add_state gets wrong for this profile:
it appends `ratings:` at end-of-file, but config-ortho/quality.yaml ENDS INSIDE the
`hip_knee:` mapping, so star ratings would silently become hip/knee entries.

Rather than move the block afterwards, the ratings are stripped from the report before
add_state sees them and inserted at the right anchor here. Every file is re-parsed after
every write and the whole run is rolled back if anything stops being valid YAML -- these
configs feed a live site, and a half-applied state is worse than none.
"""
import io
import os
import re
import shutil
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
CFG = os.path.join(ROOT, "config-ortho")
FILES = ("hospitals.yaml", "metros.yaml", "geo.yaml", "quality.yaml", "contact.yaml")


def read(p):
    return io.open(p, encoding="utf-8").read()


def write(p, s):
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)


def check(step):
    for f in FILES:
        p = os.path.join(CFG, f)
        try:
            yaml.safe_load(read(p))
        except Exception as e:
            raise SystemExit(f"\n{f} stopped being valid YAML after {step}:\n  {str(e)[:200]}")


def main(argv):
    addendum = argv[1] == "--addendum"
    if addendum:                      # --addendum <report> <CODE> [extras...]
        report, code, name, slug = argv[2], argv[3], None, None
        extras = argv[4:]
    else:
        report, code, name, slug = argv[1], argv[2], argv[3], argv[4]
        extras = argv[5:]

    backup = tempfile.mkdtemp(prefix="ortho-cfg-")
    for f in FILES:
        shutil.copy2(os.path.join(CFG, f), backup)
    print(f"backup: {backup}")

    try:
        body = read(report)
        block = re.search(r"```yaml\s*(.*?)```", body, re.S).group(1)
        data = yaml.safe_load(block)
        ratings = data.get("ratings") or {}

        # hand add_state a report with no ratings, so it cannot append them to the wrong place
        stripped = re.sub(r"\nratings:\n(?:  [^\n]*\n)+", "\n", block)
        assert "ratings:" not in stripped, "failed to strip ratings from the report"
        tmp = os.path.join(backup, "report.md")
        write(tmp, body.replace(block, stripped))  # marker text outside the yaml survives

        import add_state
        add_state.CFG = CFG
        add_state.main(tmp, code, name, slug, (), addendum=addendum)
        check("add_state")

        if ratings:
            p = os.path.join(CFG, "quality.yaml")
            s = read(p)
            anchor = "\nhip_knee:"
            assert s.count(anchor) == 1, "quality.yaml has no single hip_knee: anchor"
            lines = "".join(f"  {i}: {v}\n" for i, v in ratings.items())
            i = s.index(anchor)
            write(p, s[:i] + f"\n  # {name} (added)\n" + lines.rstrip("\n") + s[i:])
            check("ratings insert")
            print(f"inserted {len(ratings)} star ratings into the ratings: section")

        for path in extras:
            base = os.path.basename(path).lower()
            target = ("quality.yaml" if "hip" in base
                      else "contact.yaml" if "contact" in base
                      else "geo.yaml" if base.startswith("geo") else None)
            if not target:
                raise SystemExit(f"don't know where {path} goes")
            p = os.path.join(CFG, target)
            write(p, read(p).rstrip("\n") + "\n" + read(path).rstrip("\n") + "\n")
            check(f"append {base}")
            print(f"appended {base} -> {target}")

        # nothing may be declared twice in a mapping: YAML keeps the last and hides the rest
        for f in ("geo.yaml", "contact.yaml"):
            keys = re.findall(r"^  ([A-Z0-9][A-Z0-9-]*):", read(os.path.join(CFG, f)), re.M)
            dup = {k for k in keys if keys.count(k) > 1}
            if dup:
                raise SystemExit(f"{f} has duplicate keys: {sorted(dup)[:8]}")
        print("no duplicate keys in geo.yaml or contact.yaml")

    except BaseException:
        for f in FILES:
            shutil.copy2(os.path.join(backup, f), os.path.join(CFG, f))
        print("\nROLLED BACK — config-ortho is untouched")
        raise


if __name__ == "__main__":
    main(sys.argv)
