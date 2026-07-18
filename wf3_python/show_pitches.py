"""
Read-only: print the generated pitch (subject + body preview) for QUALIFIED mock leads.
Run:  python show_pitches.py
"""
import sys
from psycopg2.extras import RealDictCursor
import db

# make sure Arabic/Chinese print correctly on Windows terminals
sys.stdout.reconfigure(encoding="utf-8")


def main():
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT c.legal_name, l.icp_segment, l.pitch_lang,
                       l.pitch_subject, LEFT(l.pitch_body, 220) AS body
                FROM leads l JOIN companies c ON l.company_id = c.id
                WHERE l.status = 'QUALIFIED' AND c.source = 'mock'
                ORDER BY l.pitch_lang, c.legal_name;
                """
            )
            for r in cur.fetchall():
                print("=" * 70)
                print(f"{r['legal_name']}  |  segment {r['icp_segment']}  |  lang={r['pitch_lang']}")
                print(f"SUBJECT: {r['pitch_subject']}")
                print(f"BODY:    {r['body']}")
            print("=" * 70)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
