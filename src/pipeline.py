"""Grab-and-ingest driver: read the hospital manifest, fetch each MRF (or use a
local file), run it through ingest. The "code grabbing the files and parsing"
end of the project.

    python src/pipeline.py                 # uses config/ defaults, writes data/ + reports/

ponytail: downloads are plain urllib (stdlib) — no requests/httpx dependency for
a GET-to-disk. Add retry/backoff or concurrency only when real multi-hundred-MB
hospital files make serial download the bottleneck.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
import yaml

from ingest import ingest_mrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")   # some WAFs 403 anything that says "compatible; ...bot"
SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.0 Safari/605.1.15")
# Raw MRFs larger than this are not kept between runs (disk is the constraint, not bandwidth).
RAW_CACHE_MAX = int(os.environ.get("RAW_CACHE_MAX_MB", "0")) * 1024 * 1024   # 0 = cache nothing (50 GB VPS)


def fetch_mrf(url: str, dest: str, insecure: bool = False) -> str:
    """Download an MRF to dest, reusing the cached copy ONLY while the server says it
    is unchanged. Returns dest path.

    The cache is keyed on the validators the server gave us last time (stored in
    `<dest>.meta`): we send If-None-Match / If-Modified-Since, and a 304 means keep
    what we have. A plain "file exists → skip" would make the monthly refresh a no-op
    forever — it would re-ingest the same bytes and never see a republished MRF.

    Sends a browser User-Agent — several MRF hosts 403 the default urllib agent —
    follows redirects (Panacea-style), and streams to disk (constant memory).

    ponytail: hosts that return neither ETag nor Last-Modified (Panacea, PARA,
    Craneware) have nothing to revalidate against, so they re-download every run.
    That's the correct answer, just not the cheap one; add a size/checksum compare
    if their bandwidth ever becomes the problem.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    meta_path = dest + ".meta"
    headers = {"User-Agent": UA}
    cached = os.path.exists(dest) and os.path.exists(meta_path)
    if cached:
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta, cached = {}, False       # unreadable sidecar → treat as no cache
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

    part = dest + ".part"
    ctx = ssl._create_unverified_context() if insecure else None   # public file, broken chain
    for ua in (UA, SAFARI_UA):
        req = urllib.request.Request(url, headers={**headers, "User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=300, context=ctx) as r:   # slow .ashx hosts; ponytail: no retry yet
                # A few hosts (UCLA, MemorialCare) force Content-Encoding: gzip even when not asked.
                body = gzip.GzipFile(fileobj=r) if r.headers.get("Content-Encoding") == "gzip" else r
                with open(part, "wb") as f:
                    shutil.copyfileobj(body, f)
                validators = {"etag": r.headers.get("ETag"),
                              "last_modified": r.headers.get("Last-Modified")}
            break
        except urllib.error.HTTPError as ex:
            if ex.code == 304 and cached:                          # unchanged — keep cache
                return dest
            if ex.code == 403 and ua is UA:                        # bonsecours.com 403s Chrome UAs, serves Safari
                continue
            raise
    # Only now swap in the new file, so a failed fetch leaves the good cache intact.
    os.replace(part, dest)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(validators, f)
    return dest


def _zip_member(zip_path: str, hid: str) -> str:
    """Name of the largest .csv/.json inside a downloaded MRF zip (some hospitals ship
    their CMS file zipped). It is STREAMED, never extracted — a 1.8 GB zip can hold a
    43 GB CSV (University of Utah)."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".csv", ".json"))]
        if not names:
            raise ValueError(f"{hid}: no CSV/JSON inside {zip_path}")
        return max(names, key=lambda n: z.getinfo(n).file_size)


def _resolve_source(entry: dict, raw_dir: str) -> str:
    if entry.get("file"):                            # off-box copy (bot-blocked host); zip -> (zip, member)
        path = os.path.join(ROOT, entry["file"])
        return (path, _zip_member(path, entry["id"])) if entry.get("unzip") else path
    if entry.get("url"):
        if entry.get("unzip"):                       # MRF delivered as a zip -> (zip, member)
            zpath = os.path.join(raw_dir, f"{entry['id']}.zip")
            fetch_mrf(entry["url"], zpath, entry.get("insecure", False))
            return zpath, _zip_member(zpath, entry["id"])
        ext = "json" if entry.get("format") == "json" else "csv"
        dest = os.path.join(raw_dir, f"{entry['id']}.{ext}")
        return fetch_mrf(entry["url"], dest, entry.get("insecure", False))
    raise ValueError(f"manifest entry {entry.get('id')} has neither 'file' nor 'url'")


def run_manifest(manifest, allowlist, out_dir, report_dir, raw_dir, payer_dir=None,
                 only_state=None, only_ids=None):
    with open(manifest, encoding="utf-8") as f:
        hospitals = yaml.safe_load(f)["hospitals"]
    # Rebuild semantics: ingest APPENDs to Parquet, so clear before re-running or a
    # re-run would double-count. Default = whole manifest, whole dataset wiped (idempotent
    # weekly refresh). `only_state` = rebuild just that state's hive partition
    # (data/.../state=XX) so adding a state doesn't re-parse every other one.
    # Build into a staging dir and swap in at the END, so the live app keeps serving the
    # previous dataset for the hours a multi-state ingest can take.
    if only_state:
        only_state = only_state.upper()
        hospitals = [h for h in hospitals if str(h["state"]).upper() == only_state]
    dirs = [d for d in (out_dir, payer_dir) if d]
    if only_ids:
        # Retry specific (previously FAILED, so absent) hospitals: append straight into the
        # live dataset, no wipe/swap. Re-running an id that already has rows would double it.
        hospitals = [h for h in hospitals if h["id"] in only_ids]
        build = {d: d for d in dirs}
    else:
        build = {d: d + ".build" for d in dirs}
        for b in build.values():
            shutil.rmtree(b, ignore_errors=True)
    summaries = []
    for e in hospitals:
        try:
            src = _resolve_source(e, raw_dir)
            if isinstance(src, tuple):               # stream the member straight out of the zip
                zpath, member = src
                with zipfile.ZipFile(zpath) as z, z.open(member) as fh:
                    s = ingest_mrf(member, e["id"], e["state"], allowlist, build[out_dir],
                                   report_dir, build.get(payer_dir), stream=fh)
                src = zpath
            else:
                s = ingest_mrf(src, e["id"], e["state"], allowlist, build[out_dir], report_dir,
                               build.get(payer_dir))
            summaries.append(s)
            if e.get("url") and os.path.getsize(src) > RAW_CACHE_MAX:
                # Bound the raw cache on the shared box: big files are dropped after ingest
                # and simply re-downloaded next refresh (bandwidth is cheap, disk is not).
                for f in (src, src + ".meta"):
                    if os.path.exists(f):
                        os.remove(f)
            print(f"  {e['id']:16s} {s['state']}  kept={s['codes_kept']:4d} codes  "
                  f"dropped={s['dropped_total']:7d}")
        except Exception as ex:   # one bad hospital URL must not abort the rest
            print(f"  {e.get('id','?'):16s} FAILED: {type(ex).__name__}: {str(ex)[:80]}")
    for d, b in build.items():                      # swap: whole dataset, or one state partition
        if only_ids:
            continue
        if only_state:
            part = f"state={only_state}"
            shutil.rmtree(os.path.join(d, part), ignore_errors=True)
            if os.path.isdir(os.path.join(b, part)):
                os.makedirs(d, exist_ok=True)
                shutil.move(os.path.join(b, part), os.path.join(d, part))
        elif os.path.isdir(b):
            shutil.rmtree(d, ignore_errors=True)
            os.replace(b, d)
        shutil.rmtree(b, ignore_errors=True)
    return summaries


def main():
    """python src/pipeline.py [--state XX] [--only ID,ID,...]   (no flag = full rebuild)"""
    cfg = os.path.join(ROOT, os.environ.get("APP_CONFIG", "config"))   # config | config-ortho
    only = sys.argv[sys.argv.index("--state") + 1] if "--state" in sys.argv else None
    ids = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    run_manifest(
        manifest=os.path.join(cfg, "hospitals.yaml"),
        allowlist=os.path.join(cfg, "allowlist.yaml"),
        out_dir=os.path.join(ROOT, "data", "hospital_rates"),
        report_dir=os.path.join(ROOT, "reports"),
        raw_dir=os.path.join(ROOT, "data", "raw"),
        payer_dir=os.path.join(ROOT, "data", "payer_rates"),
        only_state=only, only_ids=ids,
    )


if __name__ == "__main__":
    main()
