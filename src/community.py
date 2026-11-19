"""Visitor submissions that never publish themselves: listing corrections/additions go to a
review queue, newsletter signups to a list. Both live in one stdlib SQLite file in the data
volume (COMMUNITY_DB, default data/community.sqlite). No external platform, nothing is sent.

Review with `python tools/review_queue.py` (list / show / approve / reject / export-signups).
Approving only marks the row; a person applies the change to config/cashpay/seed_providers.json.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KINDS = {"wrong_price": "A price is wrong", "missing": "Add a missing provider or price",
         "update": "Update or remove a listing", "other": "Something else"}
ROLES = {"patient": "Patient or family", "provider": "I work for this provider", "other": "Other"}
LIMITS = {"facility": 200, "city": 80, "procedure": 200, "price": 40, "source_url": 500, "details": 4000,
          "email": 254}


def db_path():
    return os.environ.get("COMMUNITY_DB", os.path.join(ROOT, "data", "community.sqlite"))


def _db():
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY, ts REAL, kind TEXT, role TEXT, facility TEXT, city TEXT, procedure TEXT,
        price TEXT, source_url TEXT, details TEXT, email TEXT, page TEXT, ip_hash TEXT,
        status TEXT DEFAULT 'pending', reviewed_ts REAL, review_note TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS signups (
        id INTEGER PRIMARY KEY, ts REAL, email TEXT UNIQUE, area TEXT, page TEXT, ip_hash TEXT,
        unsubscribed INTEGER DEFAULT 0)""")
    return con


def ip_hash(ip):
    """Enough to spot abuse from one address; not the address itself."""
    salt = os.environ.get("COMMUNITY_SALT", "traqqit-community")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]


def clean_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    p = urllib.parse.urlsplit(u if "://" in u else "https://" + u)
    return p.geturl() if p.scheme in ("http", "https") and "." in p.netloc else None


def submit(fields: dict, ip: str) -> int:
    """Queue one correction/listing. `fields` already validated by validate()."""
    with _db() as con:
        cur = con.execute(
            "INSERT INTO submissions (ts,kind,role,facility,city,procedure,price,source_url,details,email,page,ip_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), fields["kind"], fields["role"], fields["facility"], fields["city"], fields["procedure"],
             fields["price"], fields["source_url"], fields["details"], fields["email"], fields["page"], ip_hash(ip)))
        return cur.lastrowid


def validate(raw: dict, valid_email) -> tuple[dict | None, str]:
    """-> (fields, '') or (None, error message)."""
    f = {k: (raw.get(k) or "").strip()[:LIMITS.get(k, 300)] for k in
         ("kind", "role", "facility", "city", "procedure", "price", "source_url", "details", "email", "page")}
    if f["kind"] not in KINDS:
        return None, "Pick what kind of change this is."
    f["role"] = f["role"] if f["role"] in ROLES else "other"
    if not f["facility"]:
        return None, "Tell us which provider or hospital this is about."
    if not (f["details"] or f["price"] or f["source_url"]):
        return None, "Add the correct price, a source link, or a short description."
    url = clean_url(f["source_url"])
    if url is None:
        return None, "The source link doesn't look like a web address."
    f["source_url"] = url
    if f["email"]:
        e = valid_email(f["email"])
        if not e:
            return None, "That email address doesn't look right (it's optional)."
        f["email"] = e
    if f["page"] and not f["page"].startswith("/"):
        f["page"] = ""
    return f, ""


def signup(email: str, area: str, page: str, ip: str) -> bool:
    """Store one newsletter signup. False if the address was already on the list."""
    with _db() as con:
        try:
            con.execute("INSERT INTO signups (ts,email,area,page,ip_hash) VALUES (?,?,?,?,?)",
                        (time.time(), email, area[:40], page[:200], ip_hash(ip)))
            return True
        except sqlite3.IntegrityError:
            return False


def pending(limit=200):
    with _db() as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM submissions WHERE status='pending' ORDER BY ts LIMIT ?", (limit,))]


def review(sub_id: int, status: str, note: str = "") -> bool:
    if status not in ("approved", "rejected", "pending"):
        raise ValueError(status)
    with _db() as con:
        return con.execute("UPDATE submissions SET status=?, reviewed_ts=?, review_note=? WHERE id=?",
                           (status, time.time(), note, sub_id)).rowcount == 1
