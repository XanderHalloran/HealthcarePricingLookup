"""Review queue for listing corrections and the newsletter list (local SQLite, no platform).

    python tools/review_queue.py list
    python tools/review_queue.py show 12
    python tools/review_queue.py approve 12 "updated seed row"
    python tools/review_queue.py reject 12 "no public source"
    python tools/review_queue.py signups            # count + CSV on stdout (email,area,ts)

On the server, run it inside the container so it reads the data volume:
    docker exec -it healthcare-app python tools/review_queue.py list
Approving never publishes anything: apply the change to config/cashpay/seed_providers.json.
"""
import csv
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import community  # noqa: E402


def _ts(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        rows = community.pending()
        for r in rows:
            print(f"#{r['id']:<4} {_ts(r['ts'])}  {r['kind']:<11} {r['facility'][:40]:<40} {r['procedure'][:30]}")
        print(f"{len(rows)} pending")
    elif cmd == "show":
        with community._db() as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM submissions WHERE id=?", (int(argv[2]),)).fetchone()
        for k in r.keys():
            print(f"{k:>12}: {_ts(r[k]) if k.endswith('ts') else r[k]}")
    elif cmd in ("approve", "reject"):
        ok = community.review(int(argv[2]), "approved" if cmd == "approve" else "rejected",
                              argv[3] if len(argv) > 3 else "")
        print("ok" if ok else "no such id")
    elif cmd == "signups":
        with community._db() as con:
            rows = con.execute("SELECT email, area, ts FROM signups WHERE unsubscribed=0 ORDER BY ts").fetchall()
        w = csv.writer(sys.stdout)
        w.writerow(["email", "area", "signed_up"])
        for e, a, t in rows:
            w.writerow([e, a, _ts(t)])
        print(f"# {len(rows)} signups", file=sys.stderr)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
