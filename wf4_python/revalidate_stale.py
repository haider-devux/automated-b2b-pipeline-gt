"""
Periodic staleness re-check (blueprint 1.2: data decays; re-verify before send).

Re-validates the email of every lead waiting for outreach (QUALIFIED / QUEUED_FOR_OUTREACH) and
suppresses any that have gone invalid, so a dead address never reaches the sender. Run on a cadence
(e.g. daily), or right before a send batch. Free (dnspython MX check) — no paid verifier.

  python revalidate_stale.py
"""
from psycopg2.extras import RealDictCursor
import db
import revalidate


def main():
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, email FROM leads
                           WHERE status IN ('QUALIFIED', 'QUEUED_FOR_OUTREACH')
                             AND email IS NOT NULL
                             AND email_validation_status IN ('valid', 'unverified');""")
            rows = cur.fetchall()
        checked = suppressed = 0
        for r in rows:
            checked += 1
            if revalidate.check(r["email"]) == "invalid":
                db.suppress(conn, r["id"], "stale-invalid")   # commits
                suppressed += 1
                print(f"  suppressed {r['email']} (invalid on re-check)")
        print(f"Re-checked {checked} lead(s); suppressed {suppressed} now-invalid.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
