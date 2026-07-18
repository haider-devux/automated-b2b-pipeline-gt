"""
Tiny read-only helper: show how many leads sit in each status, plus the mock leads.
Run any time to see the state of the pipeline:  python status_report.py
"""
from psycopg2.extras import RealDictCursor
import db


def main():
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            print("Lead counts by status:")
            cur.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY status;")
            rows = cur.fetchall()
            if not rows:
                print("  (no leads in the database at all)")
            for r in rows:
                print(f"  {r['status'] or '(null)':<22} {r['n']}")

            print("\nMock leads (source='mock'):")
            cur.execute(
                """
                SELECT c.legal_name, c.region, l.status, l.icp_segment, l.pitch_lang
                FROM leads l JOIN companies c ON l.company_id = c.id
                WHERE c.source = 'mock'
                ORDER BY c.legal_name;
                """
            )
            mock = cur.fetchall()
            if not mock:
                print("  (no mock leads found)")
            for r in mock:
                print(f"  {r['legal_name']:<26} {r['region'] or '-':<5} "
                      f"{r['status']:<14} seg={r['icp_segment'] or '-':<16} lang={r['pitch_lang'] or '-'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
