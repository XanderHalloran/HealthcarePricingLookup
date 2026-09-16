"""Soft email capture: "Email me this letter / comparison".

Optional, never a wall — every price, compare, and letter is on the page already; this
just mails a copy. Transactional only: ONE email per submission; a separate, default-off
checkbox records a tips opt-in but nothing sends to it (no drip exists yet).

Store = a stdlib sqlite file in the data volume (data/capture.sqlite). Mail = stdlib
smtplib driven by env: SMTP_HOST, SMTP_PORT (465 = implicit TLS, else STARTTLS),
SMTP_USER, SMTP_PASS, SMTP_FROM. Unset -> `send()` returns False and the UI says so.
ponytail: per-IP in-memory rate limit (resets on restart); move to the sqlite table if
the app ever runs more than one worker.
"""
from __future__ import annotations

import os
import re
import smtplib
import sqlite3
import time
from collections import defaultdict, deque
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("CAPTURE_DB", os.path.join(ROOT, "data", "capture.sqlite"))
RATE_PER_HOUR = int(os.environ.get("CAPTURE_RATE_PER_HOUR", "5"))

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
_hits: dict[str, deque] = defaultdict(deque)


def valid_email(s: str | None) -> str | None:
    """Normalized address, or None if it doesn't look like one."""
    s = (s or "").strip().lower()
    return s if len(s) <= 254 and _EMAIL_RE.match(s) else None


def allow(ip: str, now: float | None = None) -> bool:
    """Sliding one-hour window per IP."""
    now = now or time.time()
    q = _hits[ip]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= RATE_PER_HOUR:
        return False
    q.append(now)
    return True


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS captures (
        id INTEGER PRIMARY KEY, ts REAL, email TEXT, kind TEXT, state TEXT, metro TEXT,
        codes TEXT, payer TEXT, optin INTEGER, ip TEXT, sent INTEGER)""")
    return con


def record(email, kind, state, metro, codes, payer, optin, ip, sent) -> int:
    with _db() as con:
        cur = con.execute(
            "INSERT INTO captures (ts,email,kind,state,metro,codes,payer,optin,ip,sent) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), email, kind, state, metro, codes, payer, int(bool(optin)), ip, int(bool(sent))))
        return cur.lastrowid


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text email. False when SMTP isn't configured or the send fails."""
    if not configured():
        return False
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465"))
    user, pw, sender = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ["SMTP_FROM"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError):
        return False


def compare_summary(rows, state_name, area_label, link) -> str:
    """Plain-text copy of a price comparison: per code the benchmark numbers + the
    cheapest facilities. Same figures the page shows; estimates, not a guarantee."""
    def m(v):
        return f"${v:,.0f}" if v is not None else "n/a"
    out = [f"Your hospital price comparison — {area_label}, {state_name}", ""]
    for r in rows:
        if not r.get("resolved"):
            continue
        d = r["result"]
        out.append(f"{r['code']}  {d.get('description') or ''}".rstrip())
        out.append(f"  What Medicare pays: {m(d.get('medicare_rate'))}   "
                   f"Typical price near you: {m(d.get('neg_median'))}   "
                   f"Ask to pay: {m(d.get('target_ask'))}")
        hosps = [h for h in d.get("hospitals", []) if h.get("price") is not None][:12]
        for h in hosps:
            flag = "  (likely error)" if h.get("outlier") == "low" else ("  (gross charge)" if h.get("outlier") == "high" else "")
            out.append(f"    {m(h['price']):>10}  {h['name']}{flag}")
        if len(d.get("hospitals", [])) > len(hosps):
            out.append(f"    … +{len(d['hospitals']) - len(hosps)} more on the site")
        out.append("")
    out += [f"See it live / negotiate: {link}", "",
            "Estimates from public Medicare benchmarks and hospital-published price files — "
            "not a guarantee, and not legal, medical, or financial advice.",
            "This is the one email you asked for. We don't sell or share your address."]
    return "\n".join(out)
