"""
One-off cleanup: disqualify already-collected leads that match the mega-brand blocklist in
targets.py (they slipped in before the blocklist existed). Safe to re-run.

  python prune.py
"""
from psycopg2.extras import RealDictCursor, Json
import db
import targets


def main():
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT l.id, l.status, c.legal_name
                           FROM leads l JOIN companies c ON l.company_id = c.id
                           WHERE l.status NOT IN ('DISQUALIFIED', 'SUPPRESSED');""")
            rows = cur.fetchall()
        pruned = 0
        for r in rows:
            if targets.is_blocked(r["legal_name"]):
                with conn.cursor() as w:
                    w.execute("""UPDATE leads SET status='DISQUALIFIED',
                                   disqualify_reason='Mega-brand / national chain - out of ICP (targeting).',
                                   updated_at=now() WHERE id=%s;""", (r["id"],))
                    w.execute("""INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail)
                                 VALUES (%s, %s, 'DISQUALIFIED', 'WF-1', %s);""",
                              (r["id"], r["status"], Json({"reason": "blocklist prune"})))
                pruned += 1
                print(f"  disqualified  {r['legal_name']}  (was {r['status']})")
        conn.commit()
        print(f"\nPruned {pruned} blocklisted lead(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
